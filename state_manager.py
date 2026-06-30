"""
state_manager.py – Zentraler App-Zustand
Ersetzt st.session_state. Ein Singleton pro Server-Prozess.
Thread-safe via asyncio.Lock (FastAPI läuft async).
"""
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd

import os as _os
_DATA_DIR = _os.getenv("DATA_DIR", "data")
_os.makedirs(_DATA_DIR, exist_ok=True)
CONFIG_FILE = _os.path.join(_DATA_DIR, "touren_config.json")
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
ALLE_TAGE = WOCHENTAGE + ["Feiertag"]   # Feiertag = eigene Tour-Konfiguration


@dataclass
class AppState:
    # --- Produkt-/Filial-Daten ---
    df: Optional[pd.DataFrame] = None
    filialen_liste: list[str] = field(default_factory=list)
    standard_filialen: list[str] = field(default_factory=list)  # nur Standard-Seiten (keine Fremdkunden)

    # --- Takt-Position ---
    selected_posten_idx: int = 0     # welcher Artikel gerade dran ist
    current_filiale_idx: int = 0     # welche Filiale innerhalb des Artikels
    produkt_fertig_sperre: bool = False

    # --- Filter ---
    kat_filter: str = "Alle"
    lieferung_phase: str = "1."   # "1." = Erstlieferung | "V" = Vorbestellung | "2." = Zweite Lieferung

    # --- Tages-Override ---
    tag_override: str = ""       # manuell gesetzt (Dropdown), leer = aus PDF oder Systemuhr
    pdf_detected_tag: str = ""   # aus PDF gelesen, bleibt auch wenn Dropdown auf "auto" steht
    pdf_detected_datum: str = ""  # Datum aus PDF-Header (z.B. '16.06.2026'), leer wenn nicht gefunden

    # --- Hardware-Modus ---
    hardware_mode: str = "VIRTUAL"   # "VIRTUAL" | "MQTT"

    # --- Virtuelle Displays (Simulation) ---
    virtual_displays: dict[str, int] = field(default_factory=dict)

    # --- Touren-Konfiguration ---
    touren_config: dict[str, list[str]] = field(default_factory=lambda: {t: [] for t in ALLE_TAGE})

    # --- Intern ---
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    # ------------------------------------------------------------------ #
    # Persistenz
    # ------------------------------------------------------------------ #
    def load_touren_config(self) -> None:
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                # sicherstellen, dass alle Tage (inkl. Feiertag) vorhanden sind
                for tag in ALLE_TAGE:
                    if tag not in loaded:
                        loaded[tag] = []
                self.touren_config = loaded
                return
            except Exception:
                pass
        self.touren_config = {t: [] for t in ALLE_TAGE}

    def save_touren_config(self) -> None:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.touren_config, f, ensure_ascii=False, indent=4)

    # ------------------------------------------------------------------ #
    # Helfer: gefilterte Daten
    # ------------------------------------------------------------------ #
    def get_df_gefiltert(self) -> pd.DataFrame:
        if self.df is None:
            return pd.DataFrame()
        df = self.df
        # Kategorie-Filter
        if self.kat_filter != "Alle":
            df = df[df["Kat"] == self.kat_filter]
        # Lieferungs-Phasen-Filter
        df = df[df["Typ"] == self.lieferung_phase]
        return df

    def get_filialen_geordnet(self, heute_tag: str, use_tour: bool = True) -> list[str]:
        """Gibt die Filial-Reihenfolge für den heutigen Tag zurück.
        Tourplan konfiguriert → NUR die konfigurierten Filialen (Fremdkunden automatisch ausgeblendet).
        Kein Tourplan → nur Standard-Filialen (ohne Fremdkunden).
        """
        if use_tour and self.touren_config.get(heute_tag):
            # Nur Filialen die im Tourplan UND im PDF stehen
            return [f for f in self.touren_config[heute_tag] if f in self.filialen_liste]
        # Kein Tourplan: Standard-Filialen als Fallback (keine Fremdkunden)
        return self.standard_filialen

    def get_kategorien(self) -> list[str]:
        if self.df is None:
            return []
        return sorted(self.df["Kat"].unique().tolist())

    TYP_LABELS = {"1.": "Erstlieferung", "V": "Vorbestellung", "2.": "2. Lieferung"}

    def get_posten_labels(self, df_gefiltert: pd.DataFrame) -> list[str]:
        labels = []
        for _, r in df_gefiltert.iterrows():
            typ_str = self.TYP_LABELS.get(str(r["Typ"]), str(r["Typ"]))
            labels.append(f"Nr. {r['Nr']} - {r['Name']} [{typ_str}]")
        return labels

    def zeile_fertig(self, idx, filialen: list[str]) -> bool:
        """True, wenn ALLE relevanten Filialen (Soll>0) dieser df-Zeile geliefert
        sind. Beim Nachlegen wird _Geliefert auf 0 gesetzt -> nicht mehr fertig;
        nach erneutem Packen ist _Geliefert wieder >0 -> wieder fertig (gruen)."""
        if self.df is None or idx not in self.df.index:
            return False
        row = self.df.loc[idx]
        rel = [f for f in filialen if float(row.get(f, 0) or 0) > 0]
        if not rel:
            return False
        for f in rel:
            gc = f"{f}_Geliefert"
            gel = float(self.df.at[idx, gc]) if gc in self.df.columns else 0.0
            if gel != gel:  # NaN
                gel = 0.0
            if gel <= 0:
                return False
        return True

    # ------------------------------------------------------------------ #
    # Takt-Logik
    # ------------------------------------------------------------------ #
    def reset_morning(self, filialen: list[str]) -> None:
        """Gesamten Morgen zurücksetzen."""
        if self.df is not None:
            for f in filialen:
                col = f"{f}_Geliefert"
                if col in self.df.columns:
                    self.df[col] = 0.0
                nl_col = f"{f}_Nachlege"
                if nl_col in self.df.columns:
                    self.df[nl_col] = 0.0
        self.selected_posten_idx = 0
        self.current_filiale_idx = 0
        self.produkt_fertig_sperre = False
        self.virtual_displays = {}

    def load_pdf_data(self, df: pd.DataFrame, filialen: list[str],
                      standard_filialen: list[str] = None) -> None:
        """Nach PDF-Upload: State initialisieren.
        filialen       = alle Filialen aus PDF (inkl. Verkaufsautos + Fremdkunden)
        standard_filialen = nur Filialen von Standard-Seiten (Fallback wenn kein Tourplan)
        """
        for f in filialen:
            df[f"{f}_Geliefert"] = 0.0
            df[f"{f}_Nachlege"]  = 0.0
        self.df = df
        self.filialen_liste = filialen
        self.standard_filialen = standard_filialen if standard_filialen is not None else filialen
        self.selected_posten_idx = 0
        self.current_filiale_idx = 0
        self.produkt_fertig_sperre = False
        self.virtual_displays = {}

    # ------------------------------------------------------------------ #
    # Laufzeit-Persistenz (Fortschritt übersteht Server-Neustart)
    # ------------------------------------------------------------------ #
    def export_runtime(self) -> dict:
        """Serialisiert den Kommissionier-Fortschritt (Geliefert/Nachlege + Position)."""
        rows = []
        if self.df is not None:
            has_q = "Quelle" in self.df.columns
            for idx, r in self.df.iterrows():
                fil = {}
                for f in self.filialen_liste:
                    gc, nc = f"{f}_Geliefert", f"{f}_Nachlege"
                    g = float(self.df.at[idx, gc]) if gc in self.df.columns else 0.0
                    n = float(self.df.at[idx, nc]) if nc in self.df.columns else 0.0
                    if g != g:
                        g = 0.0
                    if n != n:
                        n = 0.0
                    if g or n:
                        fil[f] = [g, n]
                if fil:
                    rows.append({
                        "nr": str(r["Nr"]), "typ": str(r["Typ"]),
                        "quelle": str(r["Quelle"]) if has_q else "",
                        "fil": fil,
                    })
        return {
            "rows": rows,
            "selected_posten_idx": self.selected_posten_idx,
            "current_filiale_idx": self.current_filiale_idx,
            "produkt_fertig_sperre": self.produkt_fertig_sperre,
            "lieferung_phase": self.lieferung_phase,
            "kat_filter": self.kat_filter,
            "tag_override": self.tag_override,
            "hardware_mode": self.hardware_mode,
        }

    def restore_runtime(self, data: dict) -> None:
        """Wendet einen zuvor gespeicherten Fortschritt auf das (frisch geladene) df an."""
        if self.df is None or not data:
            return
        has_q = "Quelle" in self.df.columns
        lookup = {}
        for idx, r in self.df.iterrows():
            key = (str(r["Nr"]), str(r["Typ"]), str(r["Quelle"]) if has_q else "")
            lookup.setdefault(key, idx)
        for row in data.get("rows", []):
            key = (str(row.get("nr")), str(row.get("typ")),
                   str(row.get("quelle", "")) if has_q else "")
            idx = lookup.get(key)
            if idx is None:  # Fallback ohne Quelle
                for (n, t, _q), i in lookup.items():
                    if n == str(row.get("nr")) and t == str(row.get("typ")):
                        idx = i
                        break
            if idx is None:
                continue
            for f, gn in row.get("fil", {}).items():
                g, n = (gn + [0, 0])[:2] if isinstance(gn, list) else (gn, 0)
                gc, nc = f"{f}_Geliefert", f"{f}_Nachlege"
                if gc in self.df.columns:
                    self.df.at[idx, gc] = float(g)
                if nc in self.df.columns:
                    self.df.at[idx, nc] = float(n)
        self.selected_posten_idx = int(data.get("selected_posten_idx", 0) or 0)
        self.current_filiale_idx = int(data.get("current_filiale_idx", 0) or 0)
        self.produkt_fertig_sperre = bool(data.get("produkt_fertig_sperre", False))
        self.lieferung_phase = data.get("lieferung_phase", self.lieferung_phase)
        self.kat_filter = data.get("kat_filter", self.kat_filter)
        self.tag_override = data.get("tag_override", self.tag_override)
        self.hardware_mode = data.get("hardware_mode", self.hardware_mode)
        self.kat_filter = "Alle"
        self.lieferung_phase = "1."

    def execute_weiter(self, filialen_heute: list[str]) -> dict:
        """
        Weiter-Befehl: eine Filiale abhaken oder (wenn Sperre) nächsten Artikel starten.
        Gibt ein Event-Dict zurück, das an den WebSocket-Client gesendet wird.
        """
        if self.produkt_fertig_sperre:
            return self._naechstes_produkt(filialen_heute)
        return self._abhaken(filialen_heute)

    def execute_zurueck(self, filialen_heute: list[str]) -> dict:
        """Zurück-Befehl: eine Filiale zurückgehen."""
        self.produkt_fertig_sperre = False
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return {"event": "no_op"}

        n_posten = len(df_gef)
        if self.current_filiale_idx > 0:
            self.current_filiale_idx -= 1
        else:
            self.selected_posten_idx = (self.selected_posten_idx - 1) % n_posten
            self.current_filiale_idx = 0

        return {"event": "state_update"}

    def naechstes_produkt(self, filialen_heute: list[str]) -> dict:
        """Expliziter 'Nächstes Produkt'-Befehl nach Sperre."""
        return self._naechstes_produkt(filialen_heute)

    def bestaetige_filiale(self, filiale_name: str, filialen_heute: list[str],
                           geliefert_menge: float = None) -> dict:
        """Bestätigt eine Filiale manuell (außer der Reihe).
        geliefert_menge: tatsächlich eingepackte Menge (None = Soll-Menge).
        """
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return {"event": "no_op"}

        row = df_gef.iloc[self.selected_posten_idx]
        actual_idx = row.name
        soll_menge = row.get(filiale_name, 0)
        if soll_menge <= 0:
            return {"event": "no_op"}

        geliefert_col = f"{filiale_name}_Geliefert"
        already = float(self.df.at[actual_idx, geliefert_col] or 0) \
                  if geliefert_col in self.df.columns else 0.0
        if already > 0:
            return {"event": "no_op"}

        tatsaechlich = float(geliefert_menge) if geliefert_menge is not None else float(soll_menge)

        # Display aus, als geliefert markieren
        self._set_display(filiale_name, 0)
        self.df.at[actual_idx, geliefert_col] = tatsaechlich

        nl_col = f"{filiale_name}_Nachlege"
        nachlege_val = float(self.df.at[actual_idx, nl_col] or 0) \
                       if nl_col in self.df.columns else 0.0

        db_info = {
            "produkt_nr":   str(row["Nr"]),
            "produkt_name": str(row["Name"]),
            "filiale":      filiale_name,
            "typ":          str(row["Typ"]),
            "soll":         float(soll_menge),
            "geliefert":    tatsaechlich,
            "nachlege":     nachlege_val,
        }

        # current_filiale_idx auf erste noch nicht bestätigte Filiale setzen
        active_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
        unconfirmed = self._get_unconfirmed_indices(actual_idx, active_filialen)

        if not unconfirmed:
            self.produkt_fertig_sperre = True
            self.current_filiale_idx = len(active_filialen)
            return {"event": "produkt_fertig", "name": row["Name"], **db_info}
        else:
            self.current_filiale_idx = unconfirmed[0]
            return {"event": "filiale_erledigt", **db_info}

    def rueckgaengig_filiale(self, filiale_name: str, filialen_heute: list[str]) -> dict:
        """Macht die Bestätigung einer Filiale rückgängig."""
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return {"event": "no_op"}

        row = df_gef.iloc[self.selected_posten_idx]
        actual_idx = row.name

        geliefert_col = f"{filiale_name}_Geliefert"
        if geliefert_col not in self.df.columns:
            return {"event": "no_op"}

        # Geliefert zurücksetzen
        self.df.at[actual_idx, geliefert_col] = 0.0
        self.produkt_fertig_sperre = False

        # Display wieder einschalten wenn es die erste offene Filiale ist
        active_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
        unconfirmed = self._get_unconfirmed_indices(actual_idx, active_filialen)
        if unconfirmed:
            self.current_filiale_idx = unconfirmed[0]
            # Display anschalten für neue aktive Filiale
            curr_f = active_filialen[unconfirmed[0]]
            self._set_display(curr_f, int(row.get(curr_f, 0)))

        return {"event": "state_update", "filiale": filiale_name}

    def _get_unconfirmed_indices(self, actual_idx, active_filialen: list[str]) -> list[int]:
        """Gibt Indizes aller noch nicht bestätigten Filialen zurück."""
        result = []
        for i, f in enumerate(active_filialen):
            gc = f"{f}_Geliefert"
            geliefert = float(self.df.at[actual_idx, gc] or 0) if gc in self.df.columns else 0.0
            nl = f"{f}_Nachlege"
            nachlege = float(self.df.at[actual_idx, nl] or 0) if nl in self.df.columns else 0.0
            if geliefert == 0 or nachlege > 0:
                result.append(i)
        return result

    # ------------------------------------------------------------------ #
    # Intern
    # ------------------------------------------------------------------ #
    def _abhaken(self, filialen_heute: list[str]) -> dict:
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return {"event": "no_op"}

        row = df_gef.iloc[self.selected_posten_idx]
        active_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
        if not active_filialen:
            return {"event": "no_op"}

        curr_f = active_filialen[self.current_filiale_idx]
        actual_idx = row.name
        self.df.at[actual_idx, f"{curr_f}_Geliefert"] = row[curr_f]
        # _Nachlege bleibt als historische Info erhalten

        # Display ausschalten
        self._set_display(curr_f, 0)

        # Nachlege-Menge für DB-Eintrag auslesen
        nl_col = f"{curr_f}_Nachlege"
        nachlege_val = float(self.df.at[actual_idx, nl_col] or 0) \
                       if nl_col in self.df.columns else 0.0

        db_info = {
            "produkt_nr":   str(row["Nr"]),
            "produkt_name": str(row["Name"]),
            "filiale":      curr_f,
            "typ":          str(row["Typ"]),
            "soll":         float(row[curr_f]),
            "geliefert":    float(row[curr_f]),
            "nachlege":     nachlege_val,
        }

        self.current_filiale_idx += 1
        if self.current_filiale_idx >= len(active_filialen):
            self.produkt_fertig_sperre = True
            return {"event": "produkt_fertig", "name": row["Name"], **db_info}
        else:
            return {"event": "filiale_erledigt", "filiale": curr_f, **db_info}

    def _naechstes_produkt(self, filialen_heute: list[str]) -> dict:
        df_gef = self.get_df_gefiltert()
        n_posten = len(df_gef)
        # Alle Displays aus
        if not df_gef.empty:
            row = df_gef.iloc[self.selected_posten_idx]
            active_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
            for f in active_filialen:
                self._set_display(f, 0)

        self.produkt_fertig_sperre = False
        self.current_filiale_idx = 0
        self.selected_posten_idx = (self.selected_posten_idx + 1) % n_posten
        return {"event": "naechstes_produkt"}

    def _get_pack_filialen(self, row, filialen_heute: list[str]) -> list[str]:
        """
        Filialen für den Pack-Takt.
        Bereits bestätigte Filialen (_Geliefert > 0) werden übersprungen,
        AUSSER sie haben einen Nachlege-Auftrag (_Nachlege > 0).
        """
        result = []
        for f in filialen_heute:
            if row.get(f, 0) <= 0:
                continue
            geliefert_col = f"{f}_Geliefert"
            nachlege_col  = f"{f}_Nachlege"
            geliefert = float(self.df.at[row.name, geliefert_col] or 0) \
                        if geliefert_col in self.df.columns else 0.0
            nachlege  = float(self.df.at[row.name, nachlege_col]  or 0) \
                        if nachlege_col  in self.df.columns else 0.0
            if geliefert > 0 and nachlege == 0:
                continue   # schon fertig, kein Nachlegen → überspringen
            result.append(f)
        return result

    def _set_display(self, filiale: str, menge: int) -> None:
        """Setzt ein virtuelles Display (MQTT wird von der App-Schicht erledigt)."""
        self.virtual_displays[filiale] = menge

    def navigate_to_nachlegen(self, nr: str, filialen_heute: list[str]) -> None:
        """
        Nach Nachlegen: setzt selected_posten_idx + current_filiale_idx
        so dass der Pack-Modus auf das erste Nachlege-Filiale zeigt.
        """
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return
        for i, (_, row) in enumerate(df_gef.iterrows()):
            if str(row["Nr"]) == str(nr):
                self.selected_posten_idx = i
                alle_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
                self.current_filiale_idx = 0
                for j, f in enumerate(alle_filialen):
                    nachlege_col = f"{f}_Nachlege"
                    if nachlege_col in self.df.columns:
                        nachlege = float(self.df.at[row.name, nachlege_col] or 0)
                        if nachlege > 0:
                            self.current_filiale_idx = j
                            break
                self.produkt_fertig_sperre = False
                return

    def get_current_display_target(self, filialen_heute: list[str]) -> tuple[str, int] | None:
        """
        Gibt (filiale, menge) zurück, die gerade leuchten soll.
        None wenn kein Artikel geladen oder Sperre aktiv.
        """
        if self.produkt_fertig_sperre or self.df is None:
            return None
        df_gef = self.get_df_gefiltert()
        if df_gef.empty:
            return None
        if self.selected_posten_idx >= len(df_gef):
            return None
        row = df_gef.iloc[self.selected_posten_idx]
        active_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
        if not active_filialen:
            return None
        if self.current_filiale_idx >= len(active_filialen):
            return None
        f = active_filialen[self.current_filiale_idx]
        nl_col = f"{f}_Nachlege"
        if nl_col in self.df.columns:
            nl_val = float(self.df.at[row.name, nl_col] or 0)
            if nl_val > 0:
                return f, int(nl_val)
        return f, int(row[f])

    def to_ui_snapshot(self, filialen_heute: list[str]) -> dict:
        """
        Erzeugt einen vollständigen UI-Snapshot als dict.
        Wird bei jeder Zustandsänderung an alle WebSocket-Clients gesendet.
        """
        df_gef = self.get_df_gefiltert()
        posten_labels = self.get_posten_labels(df_gef)
        posten_done = [self.zeile_fertig(idx, filialen_heute) for idx in df_gef.index]
        kategorien = self.get_kategorien()

        current_row_data = {}
        active_filialen: list[str] = []
        filialen_status: list[dict] = []

        if not df_gef.empty and self.selected_posten_idx < len(df_gef):
            row = df_gef.iloc[self.selected_posten_idx]
            # Alle Filialen mit Soll > 0 (vollständige Liste, Reihenfolge = Index)
            alle_filialen = [f for f in filialen_heute if row.get(f, 0) > 0]
            active_filialen = alle_filialen  # alias für aktive_filiale-Berechnung unten
            current_row_data = {
                "nr": row["Nr"],
                "name": row["Name"],
                "kat": row["Kat"],
                "typ": row["Typ"],
            }
            for i, f in enumerate(alle_filialen):
                menge = int(row.get(f, 0))
                geliefert_col = f"{f}_Geliefert"
                nachlege_col  = f"{f}_Nachlege"
                geliefert_raw = self.df.at[row.name, geliefert_col] \
                               if geliefert_col in self.df.columns else 0.0
                geliefert = float(geliefert_raw) if geliefert_raw == geliefert_raw else 0.0  # NaN-safe
                nachlege_raw = self.df.at[row.name, nachlege_col] \
                               if nachlege_col in self.df.columns else 0.0
                nachlege  = float(nachlege_raw) if nachlege_raw == nachlege_raw else 0.0
                if geliefert > 0 and nachlege == 0:
                    status = "done"
                elif self.produkt_fertig_sperre or i < self.current_filiale_idx:
                    status = "done"
                elif i == self.current_filiale_idx:
                    status = "active"
                else:
                    status = "pending"
                filialen_status.append({
                    "name": f,
                    "menge": menge,           # Soll-Menge
                    "geliefert": int(geliefert) if geliefert > 0 else 0,  # tatsächlich geliefert
                    "status": status,
                })

        aktive_filiale = None
        ziel_menge = 0
        ziel_menge_nachlege = 0
        if not self.produkt_fertig_sperre and active_filialen:
            if self.current_filiale_idx < len(active_filialen):
                aktive_filiale = active_filialen[self.current_filiale_idx]
                if not df_gef.empty:
                    row = df_gef.iloc[self.selected_posten_idx]
                    gesamt = int(row.get(aktive_filiale, 0))
                    nl_col = f"{aktive_filiale}_Nachlege"
                    if nl_col in self.df.columns:
                        nl_val = int(float(self.df.at[row.name, nl_col] or 0))
                        if nl_val > 0:
                            ziel_menge_nachlege = nl_val
                            ziel_menge = gesamt - nl_val   # ursprüngliche Menge
                        else:
                            ziel_menge = gesamt
                    else:
                        ziel_menge = gesamt

        return {
            "type": "state",
            "pdf_loaded": self.df is not None,
            "hardware_mode": self.hardware_mode,
            "kat_filter": self.kat_filter,
            "lieferung_phase": self.lieferung_phase,
            "kategorien": kategorien,
            "posten_labels": posten_labels,
            "posten_done": posten_done,
            "selected_posten_idx": self.selected_posten_idx,
            "current_filiale_idx": self.current_filiale_idx,
            "produkt_fertig_sperre": self.produkt_fertig_sperre,
            "produkt_name": current_row_data.get("name", ""),
            "aktive_filiale": aktive_filiale,
            "ziel_menge": ziel_menge,
            "ziel_menge_nachlege": ziel_menge_nachlege,
            "filialen_status": filialen_status,
            "virtual_displays": self.virtual_displays,
            "touren_config": self.touren_config,
            "filialen_liste": self.filialen_liste,
            "filialen_heute": filialen_heute,
            "tag_override": self.tag_override,
            "pdf_detected_tag": self.pdf_detected_tag,
            "pdf_detected_datum": self.pdf_detected_datum,
        }


# Singleton
_state: Optional[AppState] = None


def get_state() -> AppState:
    global _state
    if _state is None:
        _state = AppState()
        _state.load_touren_config()
    return _state
