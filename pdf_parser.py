"""
pdf_parser.py – Bäckerei Versandliste Parser (v2, koordinatenbasiert)
Nutzt pdfplumber für exakte Spaltenerkennung via x-Koordinaten.
"""
import re
from collections import defaultdict
import pdfplumber
import pandas as pd


def _is_number(text):
    return bool(re.match(r'^\d+(?:[.,]\d+)?$', text))

def _to_float(text):
    return float(text.replace(',', '.'))

def _nearest_idx(x, centers):
    return min(range(len(centers)), key=lambda i: abs(centers[i] - x))

def _cluster_xs(values, tol=8.0):
    if not values:
        return []
    sv = sorted(values)
    clusters = [[sv[0]]]
    for v in sv[1:]:
        if v - clusters[-1][-1] <= tol:
            clusters[-1].append(v)
        else:
            clusters.append([v])
    return [sum(c)/len(c) for c in clusters]

def _y_key(top):
    """Rundet y auf nächste 4er-Schritte, um Nr und Name in gleicher Gruppe zu halten.
    4er statt 2er, weil auf manchen Seiten die Artikelnummer 0.4px tiefer sitzt als der Rest."""
    return round(top / 4) * 4

def _parse_header_tokens(chars, header_y, ges_x1):
    """
    Liest Filialnamen aus Header-Zeichen und gibt Liste von (name, center_x) zurück.
    Trennt zusammengeführte Wörter wie 'SchleizRegiom.' oder 'Stadtro.Pölzig'
    an Übergängen: Großbuchstabe nach Kleinbuchstabe ODER nach nicht-alphabetischem Zeichen.
    Fügt Leerzeichen ein wenn zwei Zeichengruppen sichtbar getrennt sind (gap > 2px).
    """
    hdr = sorted(
        [c for c in chars if abs(c['top'] - header_y) <= 4 and c['x0'] > ges_x1],
        key=lambda c: c['x0'],
    )
    if not hdr:
        return []

    tokens = []
    cur_text = hdr[0]['text']
    cur_x0   = hdr[0]['x0']
    cur_x1   = hdr[0]['x1']

    for c in hdr[1:]:
        gap = c['x0'] - cur_x1
        is_upper = c['text'].isupper() and len(c['text']) == 1
        # Trennung wenn: vorheriges Zeichen = Kleinbuchstabe ODER Nicht-Buchstabe
        prev_splits = cur_text and (cur_text[-1].islower() or not cur_text[-1].isalpha())

        if gap > 6 or (gap >= 0 and is_upper and prev_splits):
            tokens.append((cur_text, cur_x0, cur_x1))
            cur_text = c['text']
            cur_x0   = c['x0']
            cur_x1   = c['x1']
        else:
            if gap > 2:          # sichtbare Lücke → Leerzeichen
                cur_text += ' '
            cur_text += c['text']
            cur_x1 = c['x1']

    tokens.append((cur_text, cur_x0, cur_x1))
    return [(text, (x0 + x1) / 2) for text, x0, x1 in tokens]


def _parse_standard_page(page, global_filialen):
    words = page.extract_words(x_tolerance=3, y_tolerance=3)
    chars = page.chars
    if not words:
        return 'Unbekannt', []

    top_text = ' '.join(w['text'] for w in words if w['top'] < 20)
    kat = 'Unbekannt'
    page_default_typ = '1.'
    m = re.search(r'Versandliste\s*\|\s*([^|]+?)\s*\|', top_text)
    if m:
        kat = m.group(1).strip()
    if 'Vorbestellung' in top_text and '1. Lief' not in top_text and '2. Lief' not in top_text:
        page_default_typ = 'V'
    elif '2. Lief' in top_text and '1. Lief' not in top_text:
        page_default_typ = '2.'

    ges_word = next((w for w in words if w['text'] == 'Ges.' and 35 < w['top'] < 55), None)
    if not ges_word:
        return kat, []

    header_y = ges_word['top']
    ges_x1   = ges_word['x1']
    ges_cx   = (ges_word['x0'] + ges_x1) / 2

    # Filialnamen und Spalten-Mittelpunkte aus Header-Zeichen
    filiale_tokens = _parse_header_tokens(chars, header_y, ges_x1)
    if not filiale_tokens:
        return kat, []

    filialen   = [t[0] for t in filiale_tokens]
    fil_centers = [t[1] for t in filiale_tokens]
    all_centers = [ges_cx] + fil_centers   # Index 0 = Gesamt, 1..N = Filialen

    for f in filialen:
        global_filialen.add(f)

    # Datenzeilen gruppieren (y gerundet auf 2er-Schritte)
    row_groups = defaultdict(list)
    for w in words:
        if w['top'] <= header_y + 5:
            continue
        row_groups[_y_key(w['top'])].append(w)

    rows = []
    last_nr   = ''
    last_name = ''

    for y_key in sorted(row_groups):
        rw = sorted(row_groups[y_key], key=lambda w: w['x0'])
        if not rw:
            continue
        first = rw[0]

        # Notiz-/Mengenzeilen überspringen (≤4-stellig am linken Rand)
        if re.match(r'^\d{1,4}$', first['text']) and first['x0'] < 35:
            continue

        if re.match(r'^\d{5}$', first['text']) and first['x0'] < 30:
            # Artikel-Zeile
            nr = first['text']
            typ = page_default_typ   # Seiten-Default (V wenn Vorbestellung-Seite)
            typ_x = None
            for w in rw:
                if w['text'] in ('1.', 'V') and 160 < w['x0'] < 205:
                    typ = w['text']
                    typ_x = w['x0']
                    break

            max_name_x = typ_x if typ_x else (ges_word['x0'] - 5)
            name_parts = [w['text'] for w in rw
                          if w['x0'] > first['x1'] and w['x0'] < max_name_x - 2
                          and not _is_number(w['text'])]
            name = ' '.join(name_parts)

            em = re.search(r'\s*(1\.|V)$', name)
            if em:
                typ  = em.group(1)
                name = name[:em.start()].strip()

            last_nr   = nr
            last_name = name

            entry = {'Nr': nr, 'Name': name, 'Kat': kat, 'Typ': typ, 'Gesamt': 0.0, 'Quelle': 'standard'}
            for f in filialen:
                entry[f] = 0.0

            for w in rw:
                if not _is_number(w['text']):
                    continue
                cx = (w['x0'] + w['x1']) / 2
                if cx < ges_word['x0'] - 5:
                    continue
                idx = _nearest_idx(cx, all_centers)
                if idx == 0:
                    entry['Gesamt'] = _to_float(w['text'])
                elif idx - 1 < len(filialen):
                    entry[filialen[idx - 1]] = _to_float(w['text'])
            rows.append(entry)

        elif first['text'] == 'V' and 160 < first['x0'] < 205:
            if not last_nr:
                continue
            entry = {'Nr': last_nr, 'Name': last_name, 'Kat': kat, 'Typ': 'V', 'Gesamt': 0.0, 'Quelle': 'standard'}
            for f in filialen:
                entry[f] = 0.0
            for w in rw:
                if not _is_number(w['text']):
                    continue
                cx = (w['x0'] + w['x1']) / 2
                if cx < ges_word['x0'] - 5:
                    continue
                idx = _nearest_idx(cx, all_centers)
                if idx == 0:
                    entry['Gesamt'] = _to_float(w['text'])
                elif idx - 1 < len(filialen):
                    entry[filialen[idx - 1]] = _to_float(w['text'])
            rows.append(entry)

    return kat, rows


_SKIP_TEXTS = {
    'Seite', 'Seite:', 'Versandliste', 'Sonstige', 'Konditorei', 'Kuchen',
    'Snack', 'Brot', '|', 'Di', 'Mo', 'Tu', 'Mi', 'Do', 'Fr', 'Sa', 'So',
    'Kunden', 'Lieferung', 'Obermeister', 'Lauser', 'Pissarek', 'egneM-tmaseG',
}

def _parse_sonstige_page(page, global_filialen):
    words = page.extract_words(x_tolerance=3, y_tolerance=3)
    if not words:
        return 'Sonstige', []

    top_text = ' '.join(w['text'] for w in words if w['top'] < 20)
    kat = 'Sonstige'
    m = re.search(r'Sonstige\s*\|\s*(\S+)', top_text)
    if m:
        kat = m.group(1).strip()

    artikel = next((w for w in words if w['text'] == 'Artikel' and w['x0'] < 30), None)
    if not artikel:
        return kat, []
    data_y = artikel['top']

    hdr_words = [w for w in words
                 if 35 < w['top'] < data_y
                 and 220 < w['x0'] < 780
                 and w['text'] not in _SKIP_TEXTS
                 and not re.match(r'^\d', w['text'])]
    if not hdr_words:
        return kat, []

    hdr_x = [(w['x0'] + w['x1']) / 2 for w in hdr_words]
    col_centers = _cluster_xs(hdr_x, tol=8.0)
    if not col_centers:
        return kat, []

    col_groups = defaultdict(list)
    for w in hdr_words:
        cx = (w['x0'] + w['x1']) / 2
        idx = _nearest_idx(cx, col_centers)
        if abs(cx - col_centers[idx]) < 20:
            col_groups[idx].append((w['top'], w['text']))

    filialen = []
    for i in range(len(col_centers)):
        entries = sorted(col_groups.get(i, []), key=lambda x: x[0], reverse=True)
        name_parts = []
        for _y, text in entries:
            if text == '-':
                if name_parts:
                    name_parts[-1] += '-'
            else:
                rev = text[::-1]
                if name_parts and name_parts[-1].endswith('-'):
                    name_parts[-1] += rev
                else:
                    name_parts.append(rev)
        filiale_name = ' '.join(p for p in name_parts if p)
        filialen.append(filiale_name)
        global_filialen.add(filiale_name)

    row_groups = defaultdict(list)
    for w in words:
        if w['top'] <= data_y + 5:
            continue
        if w['x0'] > 780:
            continue
        row_groups[_y_key(w['top'])].append(w)

    rows = []
    for y_key in sorted(row_groups):
        rw = sorted(row_groups[y_key], key=lambda w: w['x0'])
        if not rw:
            continue
        first = rw[0]
        if re.match(r'^\d{1,4}$', first['text']) and first['x0'] < 35:
            continue
        if re.match(r'^\d{5}$', first['text']) and first['x0'] < 30:
            nr = first['text']
            name_parts = [w['text'] for w in rw
                          if w['x0'] > first['x1'] and w['x0'] < 230
                          and not _is_number(w['text'])]
            name = ' '.join(name_parts)
            typ = '1.'
            em = re.search(r'\s*(1\.|V)$', name)
            if em:
                typ  = em.group(1)
                name = name[:em.start()].strip()
            entry = {'Nr': nr, 'Name': name, 'Kat': kat, 'Typ': typ, 'Gesamt': 0.0, 'Quelle': 'fremdkunde'}
            for f in filialen:
                entry[f] = 0.0
            for w in rw:
                if not _is_number(w['text']):
                    continue
                cx = (w['x0'] + w['x1']) / 2
                if cx < 230:
                    continue
                idx = _nearest_idx(cx, col_centers)
                if abs(cx - col_centers[idx]) < 25:
                    entry[filialen[idx]] = _to_float(w['text'])
            rows.append(entry)

    return kat, rows


def _merge_same_artikel(df):
    """
    Fasst Zeilen mit gleicher Nr+Kat+Typ zusammen (addiert Filial-Mengen).
    Nötig weil Verkaufsautos auf separaten PDF-Seiten stehen als Standardfilialen,
    aber zum selben Artikel gehören.
    """
    if df.empty:
        return df
    meta_cols = ['Nr', 'Name', 'Kat', 'Typ', 'Gesamt', 'Quelle']
    fil_cols  = [c for c in df.columns if c not in meta_cols]
    result    = []
    for (nr, typ, quelle), group in df.groupby(['Nr', 'Typ', 'Quelle'], sort=False):
        if len(group) == 1:
            result.append(group.iloc[0].to_dict())
            continue
        merged = group.iloc[0].to_dict()
        for col in fil_cols:
            merged[col] = float(group[col].sum())
        # Gesamt neu berechnen
        merged['Gesamt'] = float(sum(merged.get(f, 0) for f in fil_cols))
        result.append(merged)
    return pd.DataFrame(result)


_TAG_KURZ = {
    'Mo': 'Montag', 'Di': 'Dienstag', 'Mi': 'Mittwoch',
    'Do': 'Donnerstag', 'Fr': 'Freitag', 'Sa': 'Samstag', 'So': 'Sonntag',
}
_TAG_LANG = {v: v for v in _TAG_KURZ.values()}

_TAG_RE = re.compile(
    r'\b(Montag|Dienstag|Mittwoch|Donnerstag|Freitag|Samstag|Sonntag'
    r'|Mo|Di|Mi|Do|Fr|Sa|So)\b'
)

def _detect_tag_from_top_text(top_text: str):
    """
    Sucht im Header-Text nach einem Wochentag (lang oder kurz).
    Gibt 'Montag' … 'Sonntag' zurück oder None wenn nichts gefunden.
    """
    m = _TAG_RE.search(top_text)
    if not m:
        return None
    found = m.group(1)
    return _TAG_LANG.get(found) or _TAG_KURZ.get(found)


def parse_baeckerei_pdf(file_path_or_buffer, debug_nr: str = None):
    """
    Liest eine Bäckerei-Versandliste (PDF) ein.
    Gibt (df, filialen_liste, erkannter_tag) zurück.
    erkannter_tag ist z.B. 'Dienstag' oder None wenn nicht im PDF gefunden.
    Wenn debug_nr gesetzt, werden alle Roh-Zeilen für diese Nr zurückgegeben (pre-merge).
    """
    all_rows = []
    global_filialen = set()
    erkannter_tag = None

    with pdfplumber.open(file_path_or_buffer) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            words = page.extract_words(x_tolerance=3, y_tolerance=3)
            if not words:
                continue
            top_text = ' '.join(w['text'] for w in words if w['top'] < 20)
            # Tag aus erstem Treffer übernehmen
            if erkannter_tag is None:
                erkannter_tag = _detect_tag_from_top_text(top_text)
            if 'Sonstige' in top_text:
                _, page_rows = _parse_sonstige_page(page, global_filialen)
            else:
                _, page_rows = _parse_standard_page(page, global_filialen)
            if debug_nr:
                for r in page_rows:
                    r['_page'] = page_idx + 1
            all_rows.extend(page_rows)

    if debug_nr:
        return [r for r in all_rows if r.get('Nr') == debug_nr], [], None

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.fillna(0.0)
        df = _merge_same_artikel(df)

    return df, sorted(list(global_filialen)), erkannter_tag
