# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run (development):**
```
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
```

**Run (Docker):**
```
docker-compose up
```

**Install dependencies:**
```
pip install -r requirements.txt
```

## Architecture

This is a **Bäckerei Pick-by-Light** commissioning system. Bakery workers use it to pack products for branch stores by following a step-by-step guided workflow. A physical MQTT-connected display can show the target quantity at each station.

### Module Overview

| File | Role |
|------|------|
| `server.py` | FastAPI app: all REST endpoints, WebSocket hub, MQTT bridge |
| `state_manager.py` | In-memory singleton `AppState` (dataclass), async-safe via `asyncio.Lock` |
| `pdf_parser.py` | Parses the bakery's Versandliste PDF into a pandas DataFrame |
| `db.py` | SQLite persistence (3 tables, write-on-confirm) |
| `kuerzungs_engine.py` | Priority-based quantity redistribution algorithm |
| `templates/index.html` | Single-file SPA frontend, communicates via WebSocket |

### State Model

`AppState` (in `state_manager.py`) is the **only source of truth at runtime**. It holds a pandas DataFrame (`state.df`) loaded from the PDF. Two dynamic columns per filiale are added on PDF load:
- `{filiale}_Geliefert` — how much was actually delivered (0 = not yet packed)
- `{filiale}_Nachlege` — top-up quantity after a Kürzung/Nachlegen operation

**State is lost on server restart** — the PDF must be reloaded via `GET /reload-pdf` or `POST /upload-pdf`.

The pack workflow advances through products and branches using `selected_posten_idx` + `current_filiale_idx`. `produkt_fertig_sperre = True` signals that all branches for the current product are done and the worker must confirm before advancing.

### WebSocket Protocol

All UI interactions go through `WS /ws`. The client sends JSON commands (`weiter`, `zurueck`, `bestaetige_filiale`, `set_posten`, `set_kat_filter`, `set_lieferung_phase`, `save_touren`, `set_tag_override`, `reset`, etc.). After each command the server broadcasts a full state snapshot to all connected clients via `push_state()`.

HTTP fallbacks exist at `POST /api/cmd` and `GET /api/state` for n8n integrations.

### Kürzungs-Engine (kuerzungs_engine.py)

When actual baked quantity differs from the PDF order (Kürzung = less, Vermehrung = more), quantities are redistributed by priority:
1. Verkaufsauto (delivery vans) — 100% always
2. Fremdkunde (external customers) — 100% always
3. Standard-Filialen Vorbestellung — 100%
4. Standard-Filialen Erstlieferung — proportional, rounded per `produkt_rundung` config

### Persistent Data (`data/` directory)

| File | Content |
|------|---------|
| `touren_config.json` | Branch delivery order per weekday (Mon–Sun + Feiertag) |
| `kuerzungs_config.json` | Branch categories (`standard`/`verkaufsauto`/`fremdkunde`) + product rounding steps |
| `pin_config.json` | PINs for `mitarbeiter` and `admin` roles (default: 1234 / 9999) |
| `kommission.db` | SQLite: `kommission_log`, `korrektur_log`, `aktions_log` |

### Lieferung Types

The PDF contains rows with `Typ` column:
- `"1."` — Erstlieferung (first delivery)
- `"V"` — Vorbestellung (pre-order)

`lieferung_phase` in state filters which type is currently being packed.

### Filiale Ordering

If a tour plan (`touren_config`) is set for today's weekday, only those branches appear in that order. Otherwise `standard_filialen` (branches from standard PDF pages, excluding Fremdkunden) are used as fallback.

### Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PDF_PATH` | `/pdf/Drucke_Artikel-Versandliste.pdf` | Path to Versandliste PDF (network share in production) |
| `DB_PATH` | `kommission.db` | SQLite file location |
| `DATA_DIR` | `data` | Directory for JSON config files |
| `MQTT_BROKER` | `localhost` | MQTT broker host |
| `MQTT_PORT` | `1883` | MQTT broker port |

---

## Session-Wissen (aus Cowork-Session, Stand 2026-06-24)

### Docker-Wichtig
- `server.py`, `pdf_parser.py`, `kuerzungs_engine.py`, `state_manager.py`, `db.py` sind **NICHT** live-gemountet → nach jeder Änderung an Python-Dateien: `docker compose down && docker compose up --build -d`
- `templates/index.html` ist live-gemountet → HTML-Änderungen wirken sofort ohne Rebuild
- `data/` und `pdf/` sind live-gemountet

### PDF-Parser Details
- Nutzt `pdfplumber` (koordinatenbasiert), NICHT pypdf (token-basiert)
- `parse_baeckerei_pdf()` gibt **3-Tuple** zurück: `(df, filialen_liste, erkannter_tag)`
- `erkannter_tag` = Wochentag aus PDF-Header (z.B. `"Dienstag"`), oder `None`
- Tagerkennung: Regex auf ersten 20px jeder Seite, sucht deutsche Wochentagnamen (lang + kurz: Mo/Di/Mi...)
- Seiten mit "Sonstige" im Header → `_parse_sonstige_page()` (Fremdkunden/Verkaufsautos)
- Alle anderen Seiten → `_parse_standard_page()`
- `_merge_same_artikel()` fasst gleiche Artikel (Nr+Typ+Quelle) zusammen, weil Verkaufsautos auf separaten Seiten stehen

### Aktiver-Tag-Logik (server.py `get_heute_tag()`)
Priorität: `tag_override` → `pdf_detected_tag` → Systemuhr (Wochentag)
- `tag_override`: manuell per Dropdown gesetzt (persistiert in State)
- `pdf_detected_tag`: wird beim PDF-Laden gesetzt, bleibt wenn Dropdown auf "Aus PDF" steht
- Dropdown "Aus PDF (automatisch)" = leerer String → löscht tag_override, fällt auf pdf_detected_tag zurück
- **Bug der behoben wurde**: Dropdown "Aus PDF" zeigte früher Systemuhr-Tag statt PDF-Tag, weil pdf_detected_tag nicht separat gespeichert wurde

### Filialen-Reihenfolge
- `touren_config.json` hat 8 Einträge: Montag–Sonntag + **Feiertag**
- `WOCHENTAGE` = 7 Tage, `ALLE_TAGE` = WOCHENTAGE + ["Feiertag"] — beide in `state_manager.py`
- **Bug der behoben wurde**: `save_touren` und `/api/touren-config` iterierten über `WOCHENTAGE` statt `ALLE_TAGE` → Feiertag konnte nicht gespeichert werden

### PIN-Auth System
- Rollen: `mitarbeiter` (PIN 1234) und `admin` (PIN 9999) — in `data/pin_config.json`
- `/api/login` POST: `{"pin": "1234"}` → Response: `{"ok": true, "rolle": "mitarbeiter"}` (**rolle**, nicht role!)
- Mitarbeiter sieht: Pack-Modus, Mengenkorrektur, Nachlegen, Lieferphase, Aktiver Tag
- Admin sieht zusätzlich: Tourenplanung-Tab, Displays-Tab, Hardware-Modus, PIN-Änderung
- CSS: `.admin-only { display:none !important; }` — JS `applyRole()` überschreibt per inline style
- **Bug der behoben wurde**: Frontend prüfte `d.role` aber Server gibt `d.rolle` zurück → falscher PIN-Fehler obwohl PIN korrekt war

### Frontend (templates/index.html) — Achtung!
- **KRITISCH**: Die Datei ist ~1440 Zeilen. Das Edit-Tool hat sie mehrfach abgeschnitten/korrumpiert. Bei großen Änderungen immer Python-Script nutzen statt direktem Edit:
  ```python
  with open('templates/index.html', 'r') as f: html = f.read()
  html = html.replace(OLD, NEW)
  with open('templates/index.html', 'w') as f: f.write(html)
  ```
- Nach jeder Änderung prüfen: `grep -c '</script>' templates/index.html` muss 1 ergeben
- JS-Syntax prüfen: `node -e "new Function(require('fs').readFileSync('templates/index.html','utf8').match(/<script>([\s\S]*?)<\/script>/)[1])"`

### WebSocket Commands (vollständige Liste)
`weiter`, `zurueck`, `bestaetige_filiale`, `set_posten`, `set_kat_filter`, `set_lieferung_phase`, `save_touren`, `set_tag_override`, `set_hardware_mode`, `save_kuerzungen`, `reset`, `ping`

### Offene Tasks (Priorität)
1. **Upload-Bug**: Nach Docker-Rebuild testen ob `/reload-pdf` und `/upload-pdf` funktionieren
2. **Git einrichten**: `git init && git add . && git commit -m "Initial"` — verhindert zukünftige Datei-Katastrophen
3. **BBN-Vorkonfiguration**: `kuerzungs_config.json` mit echten Filialkategorien vorausfüllen
4. **PDF-Pfad**: Auf echten Bäckerei-Netzwerkpfad umstellen (Umgebungsvariable `PDF_PATH`)
5. **Duplikat-Filialen**: Klären ob Handelsware-Filialen doppelt erscheinen können
6. **Tages-Auswertung**: View für Tagesabschluss / Exportfunktion
7. **Reset-Morgen**: Vor Reset DB-Eintrag sichern

### Kürzungs-Engine Detail (kuerzungs_engine.py)

**Konzept:** Wenn weniger (oder mehr) gebacken wurde als die PDF vorgibt, müssen Mengen neu verteilt werden.

**Prioritäts-Reihenfolge:**
1. `verkaufsauto` — bekommt immer 100% (Lieferwagen müssen voll sein)
2. `fremdkunde` — bekommt immer 100% (vertraglich)
3. Standard-Filialen Vorbestellung (`Typ "V"`) — 100%
4. Standard-Filialen Erstlieferung (`Typ "1."`) — Rest, proportional + gerundet

**Rundungs-Logik:** Konfigurierbar in `data/kuerzungs_config.json` unter `produkt_rundung` (z.B. `"11051": 5` = 5er-Schritte für Brötchen). Gilt nur für Standard-Erstlieferung. Komplex und fragil — nicht ohne Tests anfassen.

**API-Endpunkte:**
- `GET /api/kuerzung/config` — lädt `kuerzungs_config.json`
- `POST /api/kuerzung/config` — speichert Kategorien + Rundung
- `POST /api/kuerzung/berechnen` — berechnet neue Verteilung (noch nicht gespeichert)
- `POST /api/kuerzung/anwenden` — übernimmt Kürzung in `state.df`
- `POST /api/kuerzung/pruefen` — prüft ob bereits Filialen kommissioniert wurden (Warnung)

**Frontend Mengenkorrektur-Tab:** Zeigt alle Artikel als Tabelle, Mitarbeiter gibt Ist-Menge ein, "Berechnen" → Engine verteilt neu, Diff grün/rot/grau. Sichtbar für Mitarbeiter + Admin. `renderMengenkorrektur()` muss noch repariert werden (nutzt fälschlicherweise `s.posten_raw`).

**Nachlegen-Tab:** `{filiale}_Nachlege` Spalte im DataFrame speichert Nachlegemengen. Daten kommen von `/api/nachlegen/info` — NICHT aus `state.posten_raw` (existiert nicht im State-Snapshot).

**Offener Task:** `kuerzungs_config.json` noch nicht mit echten BBN-Filialkategorien befüllt.

### Bekannte Eigenheiten der Bäckerei-PDF
- Artikel-Versandliste heißt "Drucke_Artikel-Versandliste.pdf"
- Wochentag steht im Header der ersten Seite (erste 20px)
- Filialnamen ändern sich dynamisch je nach Seite (Kopfzeile)
- Fremdkunden und Verkaufsautos stehen auf "Sonstige"-Seiten
- Artikelnummer ist 5-stellig
- Typ "1." = Erstlieferung, "V" = Vorbestellung

### Technische Schulden / Vorsicht
- `state_manager.py`: State geht bei Server-Neustart verloren — PDF muss neu geladen werden
- `kuerzungs_engine.py`: Rundungslogik ist komplex — nicht ohne Tests anfassen
- `_prepare_filialen()` in server.py trennt Standard- von Fremdkunden-Filialen — wichtig für Tourenplanung
- HTTP-Fallback `/api/cmd` und `/api/state` existieren für n8n-Integration (noch nicht genutzt)

### Hardware & Displays

**ESP32 Pick-by-Light Konzept:**
- Jede Filiale hat eine physische Kiste mit ESP32-Display, das die Soll-Menge anzeigt
- Es leuchtet immer genau **ein** Display gleichzeitig (Pick-by-Light)
- MQTT-Topic: `baeckerei/display/[filialname_kleingeschrieben]`, Payload = Menge als String oder `"0"` für aus

**Hardware-Modi** (Umschalten per Sidebar-Button, nur Admin):
- `VIRTUAL`: Kein MQTT gesendet, virtueller Monitor in Sidebar zeigt aktives Display
- `MQTT`: Sendet an echten Broker (`localhost:1883`)

**Displays-Tab** (nur Admin, `admin-only`):
- Zeigt alle Filialen als schwarze "Kisten" mit LED-Display-Optik
- 🟠 Orange = gerade aktiv | 🟢 Grün = ausstehend | 🔴 Rot = erledigt | Dunkel = keine Menge

**`send_display(filiale, menge)` in server.py:**
- VIRTUAL-Modus: aktualisiert nur `state.virtual_displays`
- MQTT-Modus: sendet wirklich über paho-mqtt

**Admin-only Elemente** (`.admin-only`-Klasse, `applyRole()` steuert Sichtbarkeit):
- Displays-Tab, Tourenplanung-Tab, Hardware-Modus-Buttons, PIN-Änderung

### Bugs behoben (2026-06-24)
- **WebSocket nie gestartet**: `connectWS()` wurde nie aufgerufen — Fix: Aufruf in `applyRole()` eingefügt, sodass WS nach Login startet
- **Displays-Tab leer**: `renderDisplays()` suchte `#display-simulator` statt `#displays-grid` — Fix: ID korrigiert
- **Nachlegen-Tab leer**: `renderNachlegen()` verwendete `s.posten_raw` das nicht im State existiert — Fix: holt Daten jetzt von `/api/nachlegen/info`
- **Hinweismeldungen blieben nach PDF-Laden**: `nl-no-pdf`, `nl-inhalt`, `disp-no-pdf` wurden nicht in `renderState()` umgeschaltet — Fix: Toggle in `renderState()` ergänzt
- **debug-raw Endpoint 500**: `rows, _ = parse_baeckerei_pdf(...)` entpackt 2 statt 3 Werte (3-Tuple seit Tag-Erkennung) — noch nicht gefixt
