"""
server.py – FastAPI Backend für Bäckerei Pick-by-Light
Starte mit: uvicorn server:app --host 0.0.0.0 --port 8000 --reload

Endpunkte:
  GET  /                    → Haupt-UI (index.html)
  POST /upload-pdf          → PDF einlesen, State initialisieren
  WS   /ws                  → Echtzeit-Kanal (Befehle rein, State-Updates raus)
  GET  /api/state           → aktueller State als JSON (Fallback)
  POST /api/cmd             → Befehl per HTTP (Fallback)
  GET  /api/touren-config   → Touren-Konfiguration laden
  POST /api/touren-config   → Touren-Konfiguration speichern
  POST /api/hardware-mode   → VIRTUAL / MQTT umschalten
  POST /api/kat-filter      → Kategorie-Filter setzen
  POST /api/reset           → Morgen zurücksetzen
"""
import asyncio
import datetime
import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, UploadFile, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pdf_parser import parse_baeckerei_pdf
from state_manager import get_state, WOCHENTAGE, ALLE_TAGE


def _prepare_filialen(df):
    """
    Behält ALLE Zeilen (standard + fremdkunde).
    Gibt (df, alle_filialen, standard_filialen) zurück.
    alle_filialen     = alle Spalten mit Mengen > 0 (Filialen + Verkaufsautos + Fremdkunden)
    standard_filialen = nur Spalten die auf Standard-Seiten Mengen haben (kein Fremdkunde)
    """
    if df.empty:
        return df, [], []
    meta = ['Nr', 'Name', 'Kat', 'Typ', 'Gesamt', 'Quelle']
    fil_cols = [c for c in df.columns if c not in meta]
    alle = sorted([c for c in fil_cols if df[c].sum() > 0])
    if 'Quelle' in df.columns:
        df_std = df[df['Quelle'] == 'standard']
        std = sorted([c for c in fil_cols if df_std[c].sum() > 0])
    else:
        std = alle
    return df, alle, std
from kuerzungs_engine import (
    load_kuerzungs_config, save_kuerzungs_config, berechne_alle, berechne_verteilung
)
import datetime as dt
import db as db_module

DATA_DIR = os.getenv("DATA_DIR", "data")
os.makedirs(DATA_DIR, exist_ok=True)
HISTORY_FILE = os.path.join(DATA_DIR, "kuerzungs_history.json")
RUNTIME_STATE_FILE = os.path.join(DATA_DIR, "runtime_state.json")
PIN_CONFIG_FILE = os.path.join(DATA_DIR, "pin_config.json")

_DEFAULT_PINS = {"mitarbeiter": "1234", "admin": "9999", "superadmin": "0000"}

ROLE_CONFIG_FILE = os.path.join(DATA_DIR, "role_config.json")

# Alle verwaltbaren Tabs — bei neuen Modulen hier eintragen
ALL_TABS = [
    {"id": "pack",        "label": "🚀 Pack-Modus",      "locked": True},
    {"id": "menge",       "label": "⚖️ Mengenkorrektur", "locked": False},
    {"id": "nachlegen",   "label": "📦 Nachlegen",       "locked": False},
    {"id": "tour",        "label": "📅 Tourenplanung",   "locked": False},
    {"id": "displays",    "label": "🖥 Displays",        "locked": False},
    {"id": "auswertung",  "label": "📊 Auswertung",      "locked": False},
    {"id": "einstellung", "label": "⚙️ Einstellung",     "locked": False},
]

_DEFAULT_ROLE_CONFIG = {
    "mitarbeiter": ["pack", "menge", "nachlegen"],
    "admin":       ["pack", "menge", "nachlegen", "tour", "displays", "auswertung", "einstellung"],
    "superadmin":  ["pack", "menge", "nachlegen", "tour", "displays", "auswertung", "einstellung"],
}

def load_pin_config() -> dict:
    if os.path.exists(PIN_CONFIG_FILE):
        try:
            with open(PIN_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for rolle in _DEFAULT_PINS:
                if rolle not in cfg:
                    cfg[rolle] = _DEFAULT_PINS[rolle]
            return cfg
        except Exception:
            pass
    with open(PIN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_DEFAULT_PINS, f, ensure_ascii=False, indent=2)
    return dict(_DEFAULT_PINS)

def save_pin_config(cfg: dict) -> None:
    with open(PIN_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)

def load_role_config() -> dict:
    if os.path.exists(ROLE_CONFIG_FILE):
        try:
            with open(ROLE_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for rolle in _DEFAULT_ROLE_CONFIG:
                if rolle not in cfg:
                    cfg[rolle] = _DEFAULT_ROLE_CONFIG[rolle]
            return cfg
        except Exception:
            pass
    with open(ROLE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(_DEFAULT_ROLE_CONFIG, f, ensure_ascii=False, indent=2)
    return dict(_DEFAULT_ROLE_CONFIG)

def save_role_config(cfg: dict) -> None:
    with open(ROLE_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Header-Konfiguration (welche Elemente im Header sichtbar sind)
# ---------------------------------------------------------------------------
HEADER_CONFIG_FILE = os.path.join(DATA_DIR, "header_config.json")

_DEFAULT_HEADER_CONFIG = {
    "filialen": True,      # Status "x Filialen geladen"
    "phase": True,         # Lieferphasen-Umschalter (1./Vorb./2.)
    "tag": True,           # Aktiver Tag + Datum
}

def load_header_config() -> dict:
    if os.path.exists(HEADER_CONFIG_FILE):
        try:
            with open(HEADER_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            for k, v in _DEFAULT_HEADER_CONFIG.items():
                if k not in cfg:
                    cfg[k] = v
            return cfg
        except Exception:
            pass
    return dict(_DEFAULT_HEADER_CONFIG)

def save_header_config(cfg: dict) -> None:
    with open(HEADER_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Personal-Liste (pro Person ein PIN + Rolle, aus Excel/CSV-Upload)
# ---------------------------------------------------------------------------
PERSONAL_FILE = os.path.join(DATA_DIR, "personal.json")

_ROLE_ALIASES = {
    "mitarbeiter": "mitarbeiter", "ma": "mitarbeiter", "worker": "mitarbeiter",
    "mitarbeiterin": "mitarbeiter", "personal": "mitarbeiter",
    "admin": "admin", "administrator": "admin", "leitung": "admin",
    "superadmin": "superadmin", "super admin": "superadmin",
    "super-admin": "superadmin", "sa": "superadmin", "chef": "superadmin",
}

def load_personal() -> list:
    if os.path.exists(PERSONAL_FILE):
        try:
            with open(PERSONAL_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_personal(liste: list) -> None:
    with open(PERSONAL_FILE, "w", encoding="utf-8") as f:
        json.dump(liste, f, ensure_ascii=False, indent=2)

def _parse_personal_file(file_obj, filename: str) -> list:
    """
    Liest eine Personal-Liste aus CSV oder Excel.
    Erwartete Spalten (Gross-/Kleinschreibung egal): Name, PIN, Rolle.
    Rolle wird auf mitarbeiter/admin/superadmin normalisiert.
    """
    import pandas as pd
    fn = (filename or "").lower()
    if fn.endswith(".csv"):
        df = pd.read_csv(file_obj, dtype=str, sep=None, engine="python")
    elif fn.endswith((".xlsx", ".xls")):
        df = pd.read_excel(file_obj, dtype=str)
    else:
        raise ValueError("Nur CSV oder Excel (.xlsx) erlaubt.")

    df.columns = [str(c).strip().lower() for c in df.columns]

    def find_col(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    c_name = find_col("name", "mitarbeiter", "person", "vorname")
    c_pin  = find_col("pin", "passwort", "password", "code", "kennwort")
    c_role = find_col("rolle", "role", "funktion", "berechtigung")
    if not c_pin:
        raise ValueError("Spalte 'PIN' fehlt.")
    if not c_role:
        raise ValueError("Spalte 'Rolle' fehlt.")

    out, seen_pins = [], set()
    for _, row in df.iterrows():
        pin = str(row[c_pin]).strip()
        if not pin or pin.lower() == "nan":
            continue
        if pin in seen_pins:
            raise ValueError(f"PIN '{pin}' kommt mehrfach vor — PINs muessen eindeutig sein.")
        seen_pins.add(pin)
        rolle_raw = str(row[c_role]).strip().lower()
        rolle = _ROLE_ALIASES.get(rolle_raw, "mitarbeiter")
        name = str(row[c_name]).strip() if c_name else ""
        if name.lower() == "nan":
            name = ""
        out.append({"name": name, "pin": pin, "rolle": rolle})
    return out

def load_history() -> list:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

def save_history_entry(entry: dict) -> None:
    history = load_history()
    history.insert(0, entry)   # Neueste zuerst
    history = history[:200]    # Max 200 Einträge
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ---------------------------------------------------------------------------
# MQTT (optional – wird nur im MQTT-Modus genutzt)
# ---------------------------------------------------------------------------
try:
    import paho.mqtt.client as mqtt_lib
    MQTT_AVAILABLE = True
except ImportError:
    MQTT_AVAILABLE = False

MQTT_BROKER = os.getenv("MQTT_BROKER", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

# Netzwerkordner – Pfad zur Versandliste PDF
PDF_PATH = os.getenv("PDF_PATH", "/pdf/Drucke_Artikel-Versandliste.pdf")


# Merkt sich den zuletzt beleuchteten Platz, damit beim Stationswechsel
# genau EIN Display leuchtet (Pick-by-Light): der alte Platz wird auf "0" gesetzt.
_last_display_platz: int | None = None


def mqtt_send(filiale: str, menge: int) -> None:
    """Sendet an das Display am PLATZ der Filiale (positions-basiert).

    Die Displays haengen an festen Plaetzen; die heutige Tour ordnet ihnen
    Filialen zu. Platznummer = Position der Filiale in der heutigen (vollen)
    Tour-Reihenfolge, 1-basiert.
    Topic:   baeckerei/display/<platz>        (z.B. baeckerei/display/1)
    Payload: "<Filialname>|<Menge>"  bzw. "0" wenn aus.

    Beim Wechsel auf einen anderen Platz wird der zuvor aktive Platz
    ausgeschaltet, damit immer nur ein Display leuchtet.
    """
    global _last_display_platz
    if not MQTT_AVAILABLE:
        return
    try:
        state = get_state()
        tour = state.get_filialen_geordnet(get_heute_tag(), True)  # volle Tour = Platzreihenfolge
        if filiale not in tour:
            return  # kein fester Platz (z.B. Fremdkunde/Verkaufsauto) -> kein Display
        platz = tour.index(filiale) + 1
        client = mqtt_lib.Client(mqtt_lib.CallbackAPIVersion.VERSION2)
        client.connect(MQTT_BROKER, MQTT_PORT, 60)
        client.loop_start()          # Netzwerk-Loop: ohne ihn geht die Nachricht vor dem disconnect verloren
        # Vorherigen Platz ausschalten, falls die aktive Station gewechselt hat
        if _last_display_platz is not None and _last_display_platz != platz:
            client.publish(f"baeckerei/display/{_last_display_platz}", "0", qos=1, retain=True)
        topic = f"baeckerei/display/{platz}"
        payload = f"{filiale}|{menge}" if menge > 0 else "0"
        info = client.publish(topic, payload, qos=1, retain=True)  # retain: ESP zeigt nach Reconnect den aktuellen Stand
        info.wait_for_publish(timeout=2.0)   # sicherstellen, dass sie wirklich raus ist
        client.loop_stop()
        client.disconnect()
        _last_display_platz = platz if menge > 0 else None
    except Exception as exc:
        print(f"[MQTT] Fehler: {exc}")


# ---------------------------------------------------------------------------
# WebSocket-Verbindungsverwaltung
# ---------------------------------------------------------------------------
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket) -> None:
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, data: dict) -> None:
        """Sendet State-Update an alle verbundenen Clients."""
        text = json.dumps(data, ensure_ascii=False)
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------
def get_heute_tag() -> str:
    state = get_state()
    # 1. Manueller Override (Dropdown)
    if state.tag_override:
        return state.tag_override
    # 2. Aus PDF gelesen
    if state.pdf_detected_tag:
        return state.pdf_detected_tag
    # 3. Fallback: Systemuhr
    return WOCHENTAGE[datetime.datetime.now().weekday()]


def get_filialen_heute(use_tour: bool = True) -> list[str]:
    state = get_state()
    tag = get_heute_tag()
    tour = state.get_filialen_geordnet(tag, use_tour)
    block_size = int(state.block_groessen.get(tag, 0) or 0)
    return state.get_aktive_filialen(tour, block_size)


def get_block_meta() -> dict:
    """Block-Metadaten für den Snapshot (Anzahl Blöcke, aktueller Block, Bereich)."""
    state = get_state()
    tag = get_heute_tag()
    tour = state.get_filialen_geordnet(tag, True)
    block_size = int(state.block_groessen.get(tag, 0) or 0)
    bloecke, idx = state.get_block_info(tour, block_size)
    return {
        "block_aktiv": block_size > 0 and len(bloecke) > 1,
        "block_anzahl": len(bloecke),
        "block_idx": idx,
        "block_filialen": bloecke[idx] if bloecke else [],
        "block_bereich": state.kat_filter,
        # Volle Tour-Reihenfolge (NICHT block-beschraenkt) – u.a. fuer Nachlegen,
        # das die ganze Tour betreffen soll, nicht nur den aktiven Block.
        "tour_filialen": tour,
    }


def send_display(filiale: str, menge: int) -> None:
    """Sendet an virtuelles Display oder echtes MQTT je nach Modus."""
    state = get_state()
    state.virtual_displays[filiale] = menge
    if state.hardware_mode == "MQTT":
        mqtt_send(filiale, menge)


def save_runtime_state() -> None:
    """Sichert den Kommissionier-Fortschritt auf Platte (übersteht Neustart)."""
    try:
        state = get_state()
        if state.df is None:
            return
        with open(RUNTIME_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state.export_runtime(), f, ensure_ascii=False)
    except Exception as exc:
        print("[State] Speichern fehlgeschlagen:", exc)


async def push_state() -> None:
    """Erzeugt Snapshot und sendet ihn an alle Clients."""
    state = get_state()
    filialen = get_filialen_heute()
    snapshot = state.to_ui_snapshot(filialen)
    snapshot["aktiver_tag"] = get_heute_tag()
    snapshot.update(get_block_meta())
    await manager.broadcast(snapshot)
    save_runtime_state()


# ---------------------------------------------------------------------------
# App-Lifecycle
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Server] Bäckerei Pick-by-Light startet...")
    db_module.init_db()
    print("[DB] SQLite bereit.")
    # PDF automatisch laden + Fortschritt wiederherstellen
    try:
        if os.path.exists(PDF_PATH):
            df_all, _, pdf_tag, pdf_datum = parse_baeckerei_pdf(PDF_PATH)
            df, alle_filialen, std_filialen = _prepare_filialen(df_all)
            state = get_state()
            state.load_pdf_data(df, alle_filialen, standard_filialen=std_filialen)
            if pdf_tag:
                state.pdf_detected_tag = pdf_tag
            state.pdf_detected_datum = pdf_datum or ""
            print(f"[PDF] Auto-geladen: {len(df)} Zeilen, {len(alle_filialen)} Filialen.")
            if os.path.exists(RUNTIME_STATE_FILE):
                with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                    state.restore_runtime(json.load(f))
                print("[State] Fortschritt wiederhergestellt.")
        else:
            print(f"[PDF] Kein PDF unter {PDF_PATH} – manuell laden.")
    except Exception as exc:
        print("[Startup] PDF/State-Wiederherstellung fehlgeschlagen:", exc)
    yield
    save_runtime_state()
    print("[Server] Shutdown.")


app = FastAPI(title="Bäckerei Pick-by-Light", lifespan=lifespan)

# Statische Dateien (falls templates/ existiert)
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")


# ---------------------------------------------------------------------------
# HTML-Hauptseite
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def root():
    html_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return f.read()
    return HTMLResponse("<h1>index.html nicht gefunden</h1>", status_code=500)


# ---------------------------------------------------------------------------
# PDF laden (aus Netzwerkordner)
# ---------------------------------------------------------------------------
@app.get("/reload-pdf")
async def reload_pdf():
    """Liest die PDF aus dem konfigurierten Netzwerkordner."""
    if not os.path.exists(PDF_PATH):
        raise HTTPException(404, f"PDF nicht gefunden: {PDF_PATH}")
    try:
        df_all, _, pdf_tag, pdf_datum = parse_baeckerei_pdf(PDF_PATH)
    except Exception as exc:
        raise HTTPException(500, f"PDF-Parse-Fehler: {exc}")

    df, alle_filialen, std_filialen = _prepare_filialen(df_all)

    state = get_state()
    async with state._lock:
        state.load_pdf_data(df, alle_filialen, standard_filialen=std_filialen)
        if pdf_tag:
            state.pdf_detected_tag = pdf_tag
        state.pdf_detected_datum = pdf_datum or ""

    filialen_heute = get_filialen_heute()
    target = state.get_current_display_target(filialen_heute)
    if target:
        send_display(target[0], target[1])

    db_module.log_aktion("pdf_geladen", {"pfad": PDF_PATH, "filialen": alle_filialen, "zeilen": len(df), "tag": pdf_tag, "datum": pdf_datum})
    await push_state()
    return {"ok": True, "filialen": alle_filialen, "std_filialen": std_filialen, "zeilen": len(df), "pdf_path": PDF_PATH, "tag": pdf_tag, "datum": pdf_datum}


# ---------------------------------------------------------------------------
# PDF-Upload (Fallback – für Tests ohne Netzwerkordner)
# ---------------------------------------------------------------------------
@app.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Nur PDF-Dateien erlaubt.")
    try:
        df_all, _, pdf_tag, pdf_datum = parse_baeckerei_pdf(file.file)
    except Exception as exc:
        raise HTTPException(500, f"PDF-Parse-Fehler: {exc}")

    df, alle_filialen, std_filialen = _prepare_filialen(df_all)

    state = get_state()
    async with state._lock:
        state.load_pdf_data(df, alle_filialen, standard_filialen=std_filialen)
        if pdf_tag:
            state.pdf_detected_tag = pdf_tag
        state.pdf_detected_datum = pdf_datum or ""

    filialen_heute = get_filialen_heute()
    target = state.get_current_display_target(filialen_heute)
    if target:
        send_display(target[0], target[1])

    db_module.log_aktion("pdf_upload", {"datei": file.filename, "filialen": alle_filialen, "zeilen": len(df), "tag": pdf_tag, "datum": pdf_datum})
    await push_state()
    return {"ok": True, "filialen": alle_filialen, "std_filialen": std_filialen, "zeilen": len(df), "tag": pdf_tag, "datum": pdf_datum}


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    state = get_state()

    # Sofort aktuellen State senden
    filialen = get_filialen_heute()
    init_snap = state.to_ui_snapshot(filialen)
    init_snap["aktiver_tag"] = get_heute_tag()
    init_snap.update(get_block_meta())
    await ws.send_text(json.dumps(init_snap, ensure_ascii=False))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            cmd = msg.get("cmd", "")
            filialen = get_filialen_heute()

            async with state._lock:
                if cmd == "weiter":
                    result = state.execute_weiter(filialen)
                    if result.get("event") in ("filiale_erledigt", "produkt_fertig"):
                        jetzt = dt.datetime.now()
                        db_module.upsert_kommission(
                            datum         = jetzt.strftime("%Y-%m-%d"),
                            produkt_nr    = result["produkt_nr"],
                            produkt_name  = result["produkt_name"],
                            filiale       = result["filiale"],
                            typ           = result["typ"],
                            soll          = result["soll"],
                            geliefert     = result["geliefert"],
                            nachlege      = result["nachlege"],
                            bestaetigt_um = jetzt.strftime("%H:%M:%S"),
                        )
                        db_module.log_aktion("weiter", {
                            "event":        result["event"],
                            "produkt_nr":   result["produkt_nr"],
                            "produkt_name": result["produkt_name"],
                            "filiale":      result["filiale"],
                            "typ":          result["typ"],
                            "soll":         result["soll"],
                            "geliefert":    result["geliefert"],
                        })
                    else:
                        db_module.log_aktion("weiter", {"event": result.get("event", "no_op")})
                    target = state.get_current_display_target(filialen)
                    if target and not state.produkt_fertig_sperre:
                        send_display(target[0], target[1])

                elif cmd == "zurueck":
                    result = state.execute_zurueck(filialen)
                    db_module.log_aktion("zurueck", {
                        "posten_idx": state.selected_posten_idx,
                        "filiale_idx": state.current_filiale_idx,
                    })
                    target = state.get_current_display_target(filialen)
                    if target:
                        send_display(target[0], target[1])

                elif cmd == "naechstes_produkt":
                    result = state.naechstes_produkt(filialen)
                    db_module.log_aktion("naechstes_produkt", {
                        "neuer_posten_idx": state.selected_posten_idx,
                    })
                    target = state.get_current_display_target(filialen)
                    if target:
                        send_display(target[0], target[1])

                elif cmd == "bestaetige_filiale":
                    filiale = msg.get("filiale", "")
                    geliefert_menge = msg.get("geliefert_menge", None)
                    result = state.bestaetige_filiale(filiale, filialen, geliefert_menge)
                    if result.get("event") in ("filiale_erledigt", "produkt_fertig"):
                        jetzt = dt.datetime.now()
                        db_module.upsert_kommission(
                            datum         = jetzt.strftime("%Y-%m-%d"),
                            produkt_nr    = result["produkt_nr"],
                            produkt_name  = result["produkt_name"],
                            filiale       = result["filiale"],
                            typ           = result["typ"],
                            soll          = result["soll"],
                            geliefert     = result["geliefert"],
                            nachlege      = result["nachlege"],
                            bestaetigt_um = jetzt.strftime("%H:%M:%S"),
                        )
                        db_module.log_aktion("manuell_bestaetigt", {
                            "filiale": filiale, "geliefert": result["geliefert"], "event": result["event"],
                        })
                        save_history_entry({
                            "timestamp":     jetzt.strftime("%d.%m.%Y %H:%M"),
                            "typ":           "manuell",
                            "produkt_nr":    result["produkt_nr"],
                            "produkt_name":  result["produkt_name"],
                            "filiale":       filiale,
                            "soll":          result["soll"],
                            "geliefert":     result["geliefert"],
                            "differenz":     result["geliefert"] - result["soll"],
                        })
                    target = state.get_current_display_target(filialen)
                    if target and not state.produkt_fertig_sperre:
                        send_display(target[0], target[1])

                elif cmd == "rueckgaengig_filiale":
                    filiale = msg.get("filiale", "")
                    result = state.rueckgaengig_filiale(filiale, filialen)
                    db_module.log_aktion("rueckgaengig", {"filiale": filiale})
                    target = state.get_current_display_target(filialen)
                    if target and not state.produkt_fertig_sperre:
                        send_display(target[0], target[1])

                elif cmd == "set_posten":
                    idx = msg.get("idx", 0)
                    df_gef = state.get_df_gefiltert()
                    if 0 <= idx < len(df_gef):
                        state.selected_posten_idx = idx
                        state.current_filiale_idx = 0
                        state.produkt_fertig_sperre = False
                        target = state.get_current_display_target(filialen)
                        if target:
                            send_display(target[0], target[1])
                        db_module.log_aktion("produkt_gewaehlt", {"idx": idx})
                    result = {"event": "state_update"}

                elif cmd == "set_kat_filter":
                    kat = msg.get("kat", "Alle")
                    state.kat_filter = kat
                    state.selected_posten_idx = 0
                    state.current_filiale_idx = 0
                    state.produkt_fertig_sperre = False
                    state.current_block_idx = 0
                    db_module.log_aktion("filter_kategorie", {"kat": kat})
                    result = {"event": "state_update"}

                elif cmd == "set_lieferung_phase":
                    phase = msg.get("phase", "1.")
                    state.lieferung_phase = phase
                    state.selected_posten_idx = 0
                    state.current_filiale_idx = 0
                    state.produkt_fertig_sperre = False
                    state.current_block_idx = 0
                    db_module.log_aktion("filter_phase", {"phase": phase})
                    result = {"event": "state_update"}

                elif cmd == "set_block":
                    state.current_block_idx = max(0, int(msg.get("idx", 0) or 0))
                    state.selected_posten_idx = 0
                    state.current_filiale_idx = 0
                    state.produkt_fertig_sperre = False
                    filialen = get_filialen_heute()
                    target = state.get_current_display_target(filialen)
                    if target:
                        send_display(target[0], target[1])
                    db_module.log_aktion("block_gewechselt", {"idx": state.current_block_idx})
                    result = {"event": "state_update"}

                elif cmd == "set_hardware_mode":
                    mode = msg.get("mode", "VIRTUAL")
                    state.hardware_mode = mode
                    db_module.log_aktion("hardware_mode", {"mode": mode})
                    result = {"event": "state_update"}

                elif cmd == "reset":
                    state.reset_morning(filialen)
                    db_module.log_aktion("reset_morgen", {})
                    result = {"event": "state_update"}

                elif cmd == "save_touren":
                    new_config = msg.get("config", {})
                    for tag in ALLE_TAGE:
                        if tag in new_config:
                            state.touren_config[tag] = new_config[tag]
                    state.save_touren_config()
                    new_bloecke = msg.get("bloecke")
                    if isinstance(new_bloecke, dict):
                        for tag in ALLE_TAGE:
                            if tag in new_bloecke:
                                try:
                                    state.block_groessen[tag] = max(0, int(new_bloecke[tag] or 0))
                                except Exception:
                                    state.block_groessen[tag] = 0
                        state.save_block_config()
                    db_module.log_aktion("touren_gespeichert", {"config": new_config, "bloecke": new_bloecke})
                    result = {"event": "touren_gespeichert"}

                elif cmd == "set_tag_override":
                    tag = msg.get("tag", "")
                    state.tag_override = tag if tag in ALLE_TAGE else ""
                    db_module.log_aktion("tag_override", {"tag": state.tag_override})
                    result = {"event": "state_update"}

                elif cmd == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))
                    continue

                else:
                    db_module.log_aktion("unbekannt", {"cmd": cmd})
                    result = {"event": "unknown_cmd", "cmd": cmd}

            # State an alle senden
            await push_state()

    except WebSocketDisconnect:
        manager.disconnect(ws)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Auth / PIN
# ---------------------------------------------------------------------------
class LoginBody(BaseModel):
    pin: str

class PinUpdateBody(BaseModel):
    admin_pin: str          # zur Verifikation: aktueller Admin-PIN
    rolle: str              # "mitarbeiter" | "admin"
    neuer_pin: str

@app.post("/api/login")
async def api_login(body: LoginBody):
    pin = body.pin.strip()
    # 1. Personal-Liste (eigener PIN pro Person, aus Upload)
    for p in load_personal():
        if str(p.get("pin", "")).strip() == pin:
            return {"ok": True, "rolle": p.get("rolle", "mitarbeiter"), "name": p.get("name", "")}
    # 2. Fallback: feste Rollen-PINs (abwaertskompatibel)
    cfg = load_pin_config()
    if pin == cfg.get("superadmin"):
        return {"ok": True, "rolle": "superadmin"}
    if pin == cfg.get("admin"):
        return {"ok": True, "rolle": "admin"}
    if pin == cfg.get("mitarbeiter"):
        return {"ok": True, "rolle": "mitarbeiter"}
    return JSONResponse({"ok": False, "fehler": "Falscher PIN"}, status_code=401)


# ---------------------------------------------------------------------------
# Personal-Liste Endpunkte
# ---------------------------------------------------------------------------
@app.post("/api/personal/upload")
async def upload_personal(file: UploadFile = File(...)):
    try:
        liste = _parse_personal_file(file.file, file.filename)
    except Exception as exc:
        raise HTTPException(400, f"Fehler beim Einlesen: {exc}")
    if not liste:
        raise HTTPException(400, "Keine gueltigen Eintraege gefunden (Spalten Name, PIN, Rolle?).")
    save_personal(liste)
    db_module.log_aktion("personal_upload", {"datei": file.filename, "anzahl": len(liste)})
    return {
        "ok": True,
        "anzahl": len(liste),
        "personen": [{"name": p["name"], "rolle": p["rolle"]} for p in liste],
    }

@app.get("/api/personal")
async def get_personal():
    liste = load_personal()
    return {
        "anzahl": len(liste),
        "personen": [
            {"name": p.get("name", ""), "rolle": p.get("rolle", "mitarbeiter"),
             "pin_masked": "•" * len(str(p.get("pin", "")))}
            for p in liste
        ],
    }

@app.delete("/api/personal")
async def clear_personal():
    save_personal([])
    db_module.log_aktion("personal_geleert", {})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Header-Konfiguration Endpunkte
# ---------------------------------------------------------------------------
@app.get("/api/header-config")
async def api_get_header_config():
    return load_header_config()

@app.post("/api/header-config")
async def api_save_header_config(cfg: dict):
    # Nur bekannte Schluessel uebernehmen, als bool speichern
    clean = {k: bool(cfg.get(k, _DEFAULT_HEADER_CONFIG[k])) for k in _DEFAULT_HEADER_CONFIG}
    save_header_config(clean)
    db_module.log_aktion("header_config_gespeichert", clean)
    return {"ok": True, "config": clean}

@app.get("/api/pin-config")
async def api_get_pin_config():
    cfg = load_pin_config()
    return {rolle: "*" * len(str(pin)) for rolle, pin in cfg.items()}

@app.post("/api/pin-config")
async def api_set_pin_config(body: PinUpdateBody):
    cfg = load_pin_config()
    # Superadmin darf alles ohne Verifikation des eigenen PINs ändern
    is_superadmin = body.admin_pin.strip() == cfg.get("superadmin")
    is_admin      = body.admin_pin.strip() == cfg.get("admin")
    if not is_superadmin and not is_admin:
        return JSONResponse({"ok": False, "fehler": "PIN falsch"}, status_code=403)
    # Admin darf nur mitarbeiter/admin ändern; superadmin darf alle
    allowed = ("mitarbeiter", "admin", "superadmin") if is_superadmin else ("mitarbeiter", "admin")
    if body.rolle not in allowed:
        return JSONResponse({"ok": False, "fehler": "Ungültige Rolle"}, status_code=400)
    if len(body.neuer_pin.strip()) < 4:
        return JSONResponse({"ok": False, "fehler": "PIN muss mind. 4 Stellen haben"}, status_code=400)
    cfg[body.rolle] = body.neuer_pin.strip()
    save_pin_config(cfg)
    db_module.log_aktion("pin_geaendert", {"rolle": body.rolle})
    return {"ok": True}


# ---------------------------------------------------------------------------
# Rollen-Konfiguration (Super Admin)
# ---------------------------------------------------------------------------
@app.get("/api/role-config")
async def api_get_role_config():
    return {
        "tabs":        ALL_TABS,
        "role_config": load_role_config(),
    }

@app.post("/api/role-config")
async def api_set_role_config(cfg: dict):
    # Superadmin-Tab immer für superadmin, nie für andere erzwingen
    for rolle in ("mitarbeiter", "admin"):
        tabs = cfg.get(rolle, [])
        if "superadmin" in tabs:
            tabs.remove("superadmin")
        cfg[rolle] = tabs
    if "superadmin" not in cfg.get("superadmin", []):
        cfg.setdefault("superadmin", []).append("superadmin")
    save_role_config(cfg)
    db_module.log_aktion("role_config_gespeichert", cfg)
    return {"ok": True}


# ---------------------------------------------------------------------------
# REST-Fallbacks (für Tests ohne WebSocket)
# ---------------------------------------------------------------------------
@app.get("/api/state")
async def api_state():
    state = get_state()
    filialen = get_filialen_heute()
    snap = state.to_ui_snapshot(filialen)
    snap["aktiver_tag"] = get_heute_tag()
    snap.update(get_block_meta())
    return snap


class CmdBody(BaseModel):
    cmd: str
    idx: Optional[int] = None
    kat: Optional[str] = None
    mode: Optional[str] = None
    config: Optional[dict] = None


@app.post("/api/cmd")
async def api_cmd(body: CmdBody):
    """HTTP-Fallback für Befehle (z.B. aus n8n oder curl)."""
    state = get_state()
    filialen = get_filialen_heute()
    async with state._lock:
        if body.cmd == "weiter":
            state.execute_weiter(filialen)
        elif body.cmd == "zurueck":
            state.execute_zurueck(filialen)
        elif body.cmd == "naechstes_produkt":
            state.naechstes_produkt(filialen)
        elif body.cmd == "reset":
            state.reset_morning(filialen)
    await push_state()
    return {"ok": True}


@app.get("/api/alle-artikel")
async def get_alle_artikel():
    """Alle Artikel aus dem PDF, ungefiltert – für Mengenkorrektur."""
    state = get_state()
    if state.df is None:
        return {"artikel": [], "kategorien": []}
    df = state.df
    phase = (state.lieferung_phase or "").strip()  # "1." / "V" / "2."
    if phase and "Typ" in df.columns:
        df = df[df["Typ"] == phase]
    filialen_heute = get_filialen_heute()
    seen = set()
    artikel = []
    for idx, row in df.iterrows():
        nr = row["Nr"]
        if nr in seen:
            continue
        seen.add(nr)
        soll = 0.0
        for f in state.filialen_liste:
            try:
                val = row[f] if f in row.index else 0
                soll += float(val)
            except Exception:
                pass
        # Gepackt/Offen relativ zur heutigen Tour (passt zur 'fertig'-Logik)
        soll_heute = 0.0
        geliefert_heute = 0.0
        for f in filialen_heute:
            if f not in df.columns:
                continue
            try:
                sv = float(df.at[idx, f])
                if sv == sv:
                    soll_heute += sv
            except Exception:
                pass
            gc = f"{f}_Geliefert"
            if gc in df.columns:
                try:
                    g = float(df.at[idx, gc])
                    if g == g:
                        geliefert_heute += g
                except Exception:
                    pass
        artikel.append({
            "nr": str(nr),
            "name": str(row["Name"]),
            "kat": str(row["Kat"]),
            "soll_gesamt": soll,
            "soll_heute": soll_heute,
            "geliefert_gesamt": geliefert_heute,
            "fertig": state.zeile_fertig(idx, filialen_heute),
        })
    kategorien = sorted(df["Kat"].unique().tolist()) if not df.empty else []
    return {"artikel": artikel, "kategorien": kategorien, "phase": phase}


@app.get("/api/touren-config")
async def get_touren_config():
    return get_state().touren_config


@app.post("/api/touren-config")
async def post_touren_config(config: dict):
    state = get_state()
    for tag in ALLE_TAGE:
        if tag in config:
            state.touren_config[tag] = config[tag]
    state.save_touren_config()
    await push_state()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Kürzungsmodul
# ---------------------------------------------------------------------------
@app.get("/api/kuerzung/config")
async def get_kuerzung_config():
    return load_kuerzungs_config()


@app.post("/api/kuerzung/config")
async def post_kuerzung_config(cfg: dict):
    save_kuerzungs_config(cfg)
    return {"ok": True}


class KuerzungBody(BaseModel):
    ist_mengen: dict   # { "11051": 900, "10102": 45.5, ... }


@app.post("/api/kuerzung/berechnen")
async def post_kuerzung_berechnen(body: KuerzungBody):
    state = get_state()
    if state.df is None:
        raise HTTPException(400, "Kein PDF geladen.")

    df = state.df
    df_erst = df[df["Typ"] == "1."]
    df_vor  = df[df["Typ"] == "V"]
    filialen = state.filialen_liste
    cfg = load_kuerzungs_config()

    # Fehlende Filialen in config als "standard" eintragen
    for f in filialen:
        cfg["filial_kategorien"].setdefault(f, "standard")

    ergebnisse = berechne_alle(df_erst, df_vor, body.ist_mengen, filialen, cfg)

    # Produkt-Namen dazu
    produkt_namen = {}
    for _, row in df.iterrows():
        produkt_namen[row["Nr"]] = row["Name"]

    ergebnisse_mit_namen = {
        nr: {**erg, "name": produkt_namen.get(nr, nr)}
        for nr, erg in ergebnisse.items()
    }

    # History speichern
    timestamp = dt.datetime.now().strftime("%d.%m.%Y %H:%M")
    for nr, erg in ergebnisse_mit_namen.items():
        save_history_entry({
            "timestamp": timestamp,
            "produkt_nr": nr,
            "produkt_name": erg["name"],
            "ist_menge": erg["ist_menge"],
            "soll_gesamt": erg["soll_gesamt"],
            "differenz": erg["differenz"],
            "filialen": erg["filialen"],
        })

    return {
        "ok": True,
        "filialen": filialen,
        "ergebnisse": ergebnisse_mit_namen,
    }


class AnwendenBody(BaseModel):
    ergebnisse: dict   # gleiche Struktur wie /berechnen Response


# ---------------------------------------------------------------------------
# Nachlegen-Tab
# ---------------------------------------------------------------------------

class NachlegenBody(BaseModel):
    nr: str
    nachlege_menge: float
    filialen: list[str]


class NachlegenManuelBody(BaseModel):
    nr: str
    filialen_mengen: dict[str, float]   # { "Filiale A": 3.0, "Filiale B": 5.0 }


@app.get("/api/nachlegen/info")
async def get_nachlegen_info():
    """Alle Produkte mit Filial-Status (Soll + Geliefert) für den Nachlegen-Tab."""
    state = get_state()
    if state.df is None:
        return {"produkte": [], "kategorien": []}

    df = state.df
    filialen = [f for f in state.filialen_liste if f in df.columns]
    filialen_heute = [f for f in get_filialen_heute() if f in df.columns]

    def _sf(v) -> float:
        try:
            v = float(v)
            return 0.0 if v != v else v  # NaN -> 0
        except Exception:
            return 0.0

    # Einmaliger Durchlauf: pro Artikel-Nr die Zeilen-Indizes je Typ + Reihenfolge
    rows_by_nr: dict[str, dict[str, object]] = {}
    order: list[tuple[str, str, str]] = []
    for idx, row in df.iterrows():
        nr = str(row["Nr"])
        if nr not in rows_by_nr:
            rows_by_nr[nr] = {}
            order.append((nr, str(row["Name"]), str(row["Kat"])))
        rows_by_nr[nr][str(row["Typ"])] = idx

    produkte = []
    for nr, name, kat in order:
        typ_rows = rows_by_nr[nr]
        idx_erst = typ_rows.get("1.")
        idx_vor  = typ_rows.get("V")

        filialen_info = []
        for f in filialen:
            gc = f"{f}_Geliefert"
            has_gc = gc in df.columns
            soll_erst = _sf(df.at[idx_erst, f]) if idx_erst is not None else 0.0
            soll_vor  = _sf(df.at[idx_vor,  f]) if idx_vor  is not None else 0.0
            geliefert_erst = _sf(df.at[idx_erst, gc]) if (idx_erst is not None and has_gc) else 0.0
            geliefert_vor  = _sf(df.at[idx_vor,  gc]) if (idx_vor  is not None and has_gc) else 0.0
            soll_gesamt = soll_erst + soll_vor
            if soll_gesamt > 0:
                filialen_info.append({
                    "name": f,
                    "soll": soll_gesamt,
                    "soll_erst": soll_erst,
                    "soll_vor": soll_vor,
                    "geliefert": geliefert_erst + geliefert_vor,
                    "geliefert_erst": geliefert_erst,
                    "geliefert_vor": geliefert_vor,
                })

        # fertig = alle heutigen Filialen-Zeilen dieser Nr fertig kommissioniert
        rel_rows = [i for i in typ_rows.values()
                    if any(_sf(df.at[i, f]) > 0 for f in filialen_heute)]
        fertig = bool(rel_rows) and all(state.zeile_fertig(i, filialen_heute) for i in rel_rows)

        produkte.append({
            "nr": nr,
            "name": name,
            "kat": kat,
            "filialen": filialen_info,
            "fertig": fertig,
        })

    kategorien = sorted(df["Kat"].unique().tolist())
    return {"produkte": produkte, "kategorien": kategorien}


@app.post("/api/nachlegen/anwenden")
async def post_nachlegen_anwenden(body: NachlegenBody):
    """
    Verteilt nachlege_menge proportional auf die gewählten Filialen.
    Addiert auf vorhandene df-Werte, setzt _Geliefert zurück → erscheinen erneut im Pack-Modus.
    """
    state = get_state()
    if state.df is None:
        raise HTTPException(400, "Kein PDF geladen.")

    async with state._lock:
        df = state.df
        cfg = load_kuerzungs_config()
        nr = body.nr

        soll_erst: dict[str, float] = {}
        soll_vor:  dict[str, float] = {}
        for f in body.filialen:
            if f not in df.columns:
                continue
            mask_erst = (df["Nr"] == nr) & (df["Typ"] == "1.")
            if mask_erst.any():
                try:
                    soll_erst[f] = float(df.loc[mask_erst, f].iloc[0] or 0)
                except Exception:
                    pass
            mask_vor = (df["Nr"] == nr) & (df["Typ"] == "V")
            if mask_vor.any():
                try:
                    soll_vor[f] = float(df.loc[mask_vor, f].iloc[0] or 0)
                except Exception:
                    pass

        neu = berechne_verteilung(nr, body.nachlege_menge, soll_erst, soll_vor, cfg)
        neu_fd = neu["filialen"]

        for f in body.filialen:
            fd_neu = neu_fd.get(f, {})
            nachlege_erst = fd_neu.get("neu_erst", 0)
            nachlege_vor  = fd_neu.get("neu_vor",  0)
            geliefert_col = f"{f}_Geliefert"

            nachlege_col = f"{f}_Nachlege"
            mask_erst = (df["Nr"] == nr) & (df["Typ"] == "1.")
            if mask_erst.any() and nachlege_erst > 0:
                df.loc[mask_erst, f] = float(df.loc[mask_erst, f].iloc[0] or 0) + nachlege_erst
                if geliefert_col in df.columns:
                    df.loc[mask_erst, geliefert_col] = 0.0
                if nachlege_col in df.columns:
                    df.loc[mask_erst, nachlege_col] = nachlege_erst

            mask_vor = (df["Nr"] == nr) & (df["Typ"] == "V")
            if mask_vor.any() and nachlege_vor > 0:
                df.loc[mask_vor, f] = float(df.loc[mask_vor, f].iloc[0] or 0) + nachlege_vor
                if geliefert_col in df.columns:
                    df.loc[mask_vor, geliefert_col] = 0.0
                if nachlege_col in df.columns:
                    df.loc[mask_vor, nachlege_col] = nachlege_vor

        filialen_heute = get_filialen_heute()
        state.navigate_to_nachlegen(nr, filialen_heute)
        target = state.get_current_display_target(filialen_heute)
        if target:
            send_display(target[0], target[1])

        # History + SQLite
        produkt_name = ""
        mask_name = df["Nr"] == nr
        if mask_name.any():
            produkt_name = str(df.loc[mask_name, "Name"].iloc[0])
        jetzt = dt.datetime.now()
        timestamp = jetzt.strftime("%d.%m.%Y %H:%M")
        datum = jetzt.strftime("%Y-%m-%d")
        zeit  = jetzt.strftime("%H:%M:%S")
        save_history_entry({
            "timestamp": timestamp,
            "typ": "nachlegen",
            "produkt_nr": nr,
            "produkt_name": produkt_name,
            "nachlege_menge": body.nachlege_menge,
            "filialen_auswahl": body.filialen,
            "verteilung": {f: neu_fd.get(f, {}) for f in body.filialen},
        })
        for f in body.filialen:
            fd = neu_fd.get(f, {})
            ne = fd.get("neu_erst", 0)
            nv = fd.get("neu_vor",  0)
            if ne > 0:
                db_module.insert_korrektur(datum, zeit, "nachlegen",
                    nr, produkt_name, f, "1.", 0, ne)
            if nv > 0:
                db_module.insert_korrektur(datum, zeit, "nachlegen",
                    nr, produkt_name, f, "V", 0, nv)

    await push_state()
    return {"ok": True}


@app.post("/api/nachlegen/anwenden-manuell")
async def post_nachlegen_anwenden_manuell(body: NachlegenManuelBody):
    """
    Manuelles Nachlegen: exakte Mengen pro Filiale, keine automatische Verteilung.
    Addiert die angegebene Menge direkt auf df, setzt _Geliefert zurück und _Nachlege.
    """
    state = get_state()
    if state.df is None:
        raise HTTPException(400, "Kein PDF geladen.")

    async with state._lock:
        df = state.df
        nr = body.nr
        verteilung: dict[str, dict] = {}

        for f, menge in body.filialen_mengen.items():
            if menge <= 0 or f not in df.columns:
                continue
            geliefert_col = f"{f}_Geliefert"
            nachlege_col  = f"{f}_Nachlege"

            mask_erst = (df["Nr"] == nr) & (df["Typ"] == "1.")
            if mask_erst.any():
                df.loc[mask_erst, f] = float(df.loc[mask_erst, f].iloc[0] or 0) + menge
                if geliefert_col in df.columns:
                    df.loc[mask_erst, geliefert_col] = 0.0
                if nachlege_col in df.columns:
                    df.loc[mask_erst, nachlege_col] = menge
                verteilung[f] = {"neu_erst": menge, "neu_vor": 0}

            mask_vor = (df["Nr"] == nr) & (df["Typ"] == "V")
            if mask_vor.any():
                df.loc[mask_vor, f] = float(df.loc[mask_vor, f].iloc[0] or 0) + menge
                if geliefert_col in df.columns:
                    df.loc[mask_vor, geliefert_col] = 0.0
                if nachlege_col in df.columns:
                    df.loc[mask_vor, nachlege_col] = menge
                verteilung[f] = {"neu_erst": 0, "neu_vor": menge}

        filialen_heute = get_filialen_heute()
        state.navigate_to_nachlegen(nr, filialen_heute)
        target = state.get_current_display_target(filialen_heute)
        if target:
            send_display(target[0], target[1])

        # History + SQLite
        produkt_name = ""
        mask_name = df["Nr"] == nr
        if mask_name.any():
            produkt_name = str(df.loc[mask_name, "Name"].iloc[0])
        jetzt = dt.datetime.now()
        timestamp = jetzt.strftime("%d.%m.%Y %H:%M")
        datum = jetzt.strftime("%Y-%m-%d")
        zeit  = jetzt.strftime("%H:%M:%S")
        nachlege_gesamt = sum(body.filialen_mengen.values())
        save_history_entry({
            "timestamp": timestamp,
            "typ": "nachlegen",
            "produkt_nr": nr,
            "produkt_name": produkt_name,
            "nachlege_menge": nachlege_gesamt,
            "filialen_auswahl": list(body.filialen_mengen.keys()),
            "verteilung": verteilung,
            "modus": "manuell",
        })
        for f, menge in body.filialen_mengen.items():
            if menge > 0:
                vt = verteilung.get(f, {})
                typ_str = "V" if vt.get("neu_vor", 0) > 0 else "1."
                db_module.insert_korrektur(datum, zeit, "nachlegen_manuell",
                    nr, produkt_name, f, typ_str, 0, menge)

    await push_state()
    return {"ok": True}


def _ermittle_warnungen(df, ergebnisse: dict) -> list[dict]:
    """
    Prüft für jedes Produkt in ergebnisse ob bereits Filialen kommissioniert wurden.
    Gibt eine Liste von Warnungen zurück (leer = alles OK).
    """
    warnungen = []
    for nr, erg in ergebnisse.items():
        filialen_data = erg.get("filialen", {})
        ist_menge = float(erg.get("ist_menge", 0))
        bereits_gepackt: dict[str, float] = {}

        for filiale in filialen_data:
            geliefert_col = f"{filiale}_Geliefert"
            if geliefert_col not in df.columns:
                continue
            menge = 0.0
            for typ in ["1.", "V"]:
                mask = (df["Nr"] == nr) & (df["Typ"] == typ)
                if mask.any():
                    menge += float(df.loc[mask, geliefert_col].iloc[0] or 0)
            if menge > 0:
                bereits_gepackt[filiale] = menge

        if bereits_gepackt:
            total_gepackt = sum(bereits_gepackt.values())
            produkt_name = ""
            mask_name = df["Nr"] == nr
            if mask_name.any():
                produkt_name = str(df.loc[mask_name, "Name"].iloc[0])

            # Alle Filialen mit Soll > 0
            filialen_mit_soll = [
                f for f, fd in filialen_data.items()
                if fd.get("soll_erst", 0) + fd.get("soll_vor", 0) > 0
            ]
            alle_gepackt = all(f in bereits_gepackt for f in filialen_mit_soll)
            ueberschuss = max(0.0, round(ist_menge - total_gepackt, 6))

            warnungen.append({
                "nr": nr,
                "name": produkt_name,
                "bereits_gepackt": bereits_gepackt,
                "total_gepackt": total_gepackt,
                "rest_menge": ist_menge - total_gepackt,
                "alle_gepackt": alle_gepackt,
                "ueberschuss": ueberschuss,
            })
    return warnungen


@app.post("/api/kuerzung/pruefen")
async def post_kuerzung_pruefen(body: AnwendenBody):
    """Prüft ob bereits Filialen kommissioniert wurden – ohne Änderungen."""
    state = get_state()
    if state.df is None:
        return {"warnungen": []}
    return {"warnungen": _ermittle_warnungen(state.df, body.ergebnisse)}


@app.post("/api/kuerzung/anwenden")
async def post_kuerzung_anwenden(body: AnwendenBody):
    """
    Schreibt korrigierte Mengen ins state.df.
    Bereits kommissionierte Filialen werden übersprungen;
    die verbleibende Ist-Menge wird auf die Rest-Filialen neu verteilt.
    """
    state = get_state()
    if state.df is None:
        raise HTTPException(400, "Kein PDF geladen.")

    async with state._lock:
        df = state.df
        cfg = load_kuerzungs_config()

        for nr, erg in body.ergebnisse.items():
            filialen_data = erg.get("filialen", {})
            ist_menge = float(erg.get("ist_menge", 0))
            alle_filialen = [f for f in filialen_data if f in df.columns]

            # Bereits kommissionierte Filialen ermitteln
            bereits_gepackt: dict[str, float] = {}
            for filiale in alle_filialen:
                geliefert_col = f"{filiale}_Geliefert"
                if geliefert_col not in df.columns:
                    continue
                menge = 0.0
                for typ in ["1.", "V"]:
                    mask = (df["Nr"] == nr) & (df["Typ"] == typ)
                    if mask.any():
                        menge += float(df.loc[mask, geliefert_col].iloc[0] or 0)
                if menge > 0:
                    bereits_gepackt[filiale] = menge

            rest_filialen = [f for f in alle_filialen if f not in bereits_gepackt]
            rest_menge = ist_menge - sum(bereits_gepackt.values())

            if rest_filialen and rest_menge > 0:
                # Neuverteilung nur auf Rest-Filialen
                soll_erst = {f: filialen_data[f]["soll_erst"] for f in rest_filialen}
                soll_vor  = {f: filialen_data[f]["soll_vor"]  for f in rest_filialen}
                neu = berechne_verteilung(nr, rest_menge, soll_erst, soll_vor, cfg)
                neu_fd = neu["filialen"]
            else:
                neu_fd = {}

            for filiale in rest_filialen:
                fd_neu = neu_fd.get(filiale, {})
                mask_erst = (df["Nr"] == nr) & (df["Typ"] == "1.")
                if mask_erst.any():
                    df.loc[mask_erst, filiale] = fd_neu.get("neu_erst", 0)
                mask_vor = (df["Nr"] == nr) & (df["Typ"] == "V")
                if mask_vor.any():
                    df.loc[mask_vor, filiale] = fd_neu.get("neu_vor", 0)

        # Korrekturen in SQLite schreiben
        jetzt = dt.datetime.now()
        datum = jetzt.strftime("%Y-%m-%d")
        zeit  = jetzt.strftime("%H:%M:%S")
        for nr, erg in body.ergebnisse.items():
            produkt_name = ""
            mask_name = df["Nr"] == nr
            if mask_name.any():
                produkt_name = str(df.loc[mask_name, "Name"].iloc[0])
            for filiale, fd in erg.get("filialen", {}).items():
                soll_alt = fd.get("soll_erst", 0)
                soll_neu = fd.get("neu_erst", 0)
                if soll_alt != soll_neu:
                    db_module.insert_korrektur(datum, zeit, "kuerzung",
                        nr, produkt_name, filiale, "1.", soll_alt, soll_neu)
                soll_alt_v = fd.get("soll_vor", 0)
                soll_neu_v = fd.get("neu_vor", 0)
                if soll_alt_v != soll_neu_v:
                    db_module.insert_korrektur(datum, zeit, "kuerzung",
                        nr, produkt_name, filiale, "V", soll_alt_v, soll_neu_v)

        # Display aktualisieren
        state.produkt_fertig_sperre = False
        filialen_heute = get_filialen_heute()
        target = state.get_current_display_target(filialen_heute)
        if target:
            send_display(target[0], target[1])

    db_module.log_aktion("kuerzung_angewendet", {"produkte": list(body.ergebnisse.keys())})
    await push_state()
    return {"ok": True, "angewendet": list(body.ergebnisse.keys())}


@app.post("/api/kuerzung/nachlegen")
async def post_kuerzung_nachlegen(body: AnwendenBody):
    """
    Nachlegen: verteilt den Überschuss (ist_menge - bereits_gepackt) proportional
    auf alle Filialen. Die Nachlege-Menge wird auf die df-Werte ADDIERT und
    _Geliefert zurückgesetzt, damit die Filialen im Pack-Modus erneut erscheinen.
    """
    state = get_state()
    if state.df is None:
        raise HTTPException(400, "Kein PDF geladen.")

    async with state._lock:
        df = state.df
        cfg = load_kuerzungs_config()

        for nr, erg in body.ergebnisse.items():
            filialen_data = erg.get("filialen", {})
            ist_menge = float(erg.get("ist_menge", 0))
            alle_filialen = [f for f in filialen_data if f in df.columns]

            # Bereits gepackte Gesamtmenge
            total_gepackt = 0.0
            for filiale in alle_filialen:
                geliefert_col = f"{filiale}_Geliefert"
                if geliefert_col not in df.columns:
                    continue
                for typ in ["1.", "V"]:
                    mask = (df["Nr"] == nr) & (df["Typ"] == typ)
                    if mask.any():
                        total_gepackt += float(df.loc[mask, geliefert_col].iloc[0] or 0)

            ueberschuss = ist_menge - total_gepackt
            if ueberschuss <= 0:
                continue

            # Überschuss proportional auf alle Filialen verteilen
            soll_erst = {f: filialen_data[f]["soll_erst"] for f in alle_filialen}
            soll_vor  = {f: filialen_data[f]["soll_vor"]  for f in alle_filialen}
            neu = berechne_verteilung(nr, ueberschuss, soll_erst, soll_vor, cfg)
            neu_fd = neu["filialen"]

            for filiale in alle_filialen:
                fd_neu = neu_fd.get(filiale, {})
                nachlege_erst = fd_neu.get("neu_erst", 0)
                nachlege_vor  = fd_neu.get("neu_vor",  0)
                geliefert_col = f"{filiale}_Geliefert"

                mask_erst = (df["Nr"] == nr) & (df["Typ"] == "1.")
                if mask_erst.any() and nachlege_erst > 0:
                    df.loc[mask_erst, filiale] = float(df.loc[mask_erst, filiale].iloc[0]) + nachlege_erst
                    if geliefert_col in df.columns:
                        df.loc[mask_erst, geliefert_col] = 0.0  # wieder offen

                mask_vor = (df["Nr"] == nr) & (df["Typ"] == "V")
                if mask_vor.any() and nachlege_vor > 0:
                    df.loc[mask_vor, filiale] = float(df.loc[mask_vor, filiale].iloc[0]) + nachlege_vor
                    if geliefert_col in df.columns:
                        df.loc[mask_vor, geliefert_col] = 0.0  # wieder offen

        # Pack-Modus: Position auf erstes betroffenes Produkt setzen
        filialen_heute = get_filialen_heute()
        # navigate_to_nachlegen braucht eine Produkt-Nr – nimm das erste aus ergebnisse
        first_nr = next(iter(body.ergebnisse), None)
        if first_nr:
            state.navigate_to_nachlegen(first_nr, filialen_heute)
        else:
            state.current_filiale_idx = 0
            state.produkt_fertig_sperre = False
        target = state.get_current_display_target(filialen_heute)
        if target:
            send_display(target[0], target[1])

    await push_state()
    return {"ok": True}


# ---------------------------------------------------------------------------
# SQLite Export (für n8n und Tages-Auswertung)
# ---------------------------------------------------------------------------

@app.get("/api/export/daten")
async def get_export_daten():
    """Liste aller Tage mit Einträgen (max. 30)."""
    return {"daten": db_module.get_verfuegbare_daten()}


@app.get("/api/export/heute")
async def get_export_heute():
    """Alle Bestätigungen + Korrekturen von heute — für n8n."""
    datum = dt.datetime.now().strftime("%Y-%m-%d")
    return {
        "datum": datum,
        "eintraege": db_module.get_log_by_datum(datum),
        "zusammenfassung": db_module.get_log_summary(datum),
        "korrekturen": db_module.get_korrekturen_by_datum(datum),
        "aktionen": db_module.get_aktionen_by_datum(datum),
    }


@app.get("/api/export/{datum}")
async def get_export_datum(datum: str):
    """Bestätigungen + Korrekturen + Aktionen für ein Datum (YYYY-MM-DD)."""
    return {
        "datum": datum,
        "eintraege": db_module.get_log_by_datum(datum),
        "zusammenfassung": db_module.get_log_summary(datum),
        "korrekturen": db_module.get_korrekturen_by_datum(datum),
        "aktionen": db_module.get_aktionen_by_datum(datum),
    }


@app.get("/api/kuerzung/history")
async def get_history():
    return load_history()


@app.delete("/api/kuerzung/history")
async def clear_history():
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Debug-Endpoint (temporär)
# ---------------------------------------------------------------------------
@app.get("/api/debug-raw/{nr}")
async def debug_raw(nr: str):
    """Zeigt alle Roh-Zeilen (pre-merge) für eine Artikelnummer."""
    if not os.path.exists(PDF_PATH):
        return {"error": "PDF nicht gefunden"}
    rows, _, _, _ = parse_baeckerei_pdf(PDF_PATH, debug_nr=nr)
    return {"nr": nr, "zeilen": len(rows), "rows": rows}


@app.get("/api/debug-page/{page_nr}")
async def debug_page(page_nr: int):
    """Zeigt alle Wörter einer PDF-Seite mit Koordinaten."""
    import pdfplumber
    if not os.path.exists(PDF_PATH):
        return {"error": "PDF nicht gefunden"}
    with pdfplumber.open(PDF_PATH) as pdf:
        if page_nr < 1 or page_nr > len(pdf.pages):
            return {"error": f"Seite {page_nr} existiert nicht (max {len(pdf.pages)})"}
        page = pdf.pages[page_nr - 1]
        words = page.extract_words(x_tolerance=3, y_tolerance=3)
        # Nur relevante Felder
        return {
            "seite": page_nr,
            "woerter": [{"text": w["text"], "x0": round(w["x0"],1), "x1": round(w["x1"],1), "top": round(w["top"],1)} for w in words]
        }


@app.get("/api/debug-df")
async def debug_df():
    """Zeigt was im DataFrame steckt – zum Diagnosieren von Parser-Problemen."""
    state = get_state()
    if state.df is None:
        return {"error": "Kein PDF geladen"}
    df = state.df
    meta = ['Nr', 'Name', 'Kat', 'Typ', 'Gesamt', 'Quelle']
    fil_cols = [c for c in df.columns if c not in meta and not c.endswith('_Geliefert') and not c.endswith('_Nachlege')]
    # Filialen mit irgendeiner Menge > 0
    aktive_cols = [c for c in fil_cols if df[c].sum() > 0]
    # Alle eindeutigen Kat+Quelle-Kombinationen
    kat_quelle = df[['Kat', 'Quelle']].drop_duplicates().to_dict('records') if 'Quelle' in df.columns else []
    # Erste 5 Zeilen
    sample = df[['Nr', 'Name', 'Kat', 'Typ'] + (['Quelle'] if 'Quelle' in df.columns else [])].head(10).to_dict('records')
    # Artikel mit Verkaufsauto-Mengen
    va_filialen = ["Citroen", "Benz 4", "Reno 1", "Opel", "WT"]
    va_artikel = []
    for _, row in df.iterrows():
        va_sum = sum(float(row.get(f, 0)) for f in va_filialen if f in df.columns)
        if va_sum > 0:
            va_artikel.append({
                "Nr": row["Nr"], "Name": row["Name"],
                "Kat": row["Kat"], "Typ": row["Typ"],
                **{f: float(row.get(f, 0)) for f in va_filialen if f in df.columns}
            })

    return {
        "zeilen_gesamt": len(df),
        "filialen_liste": state.filialen_liste,
        "alle_filial_spalten": fil_cols,
        "aktive_filial_spalten": aktive_cols,
        "kat_quelle_kombinationen": kat_quelle,
        "sample_zeilen": sample,
        "verkaufsauto_artikel": va_artikel,
    }
