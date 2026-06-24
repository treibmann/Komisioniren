"""
db.py – SQLite Persistenz für Kommissionier-Log

Tabelle kommission_log:
  Jede bestätigte Filiale wird sofort (live) eingetragen.
  UNIQUE auf (datum, produkt_nr, filiale, typ) → UPDATE bei Nachlegen.
"""
import sqlite3
import os

DB_FILE = os.getenv("DB_PATH", "kommission.db")


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Erstellt alle Tabellen falls sie noch nicht existieren."""
    with get_conn() as conn:
        # Bestätigte Lieferungen (Pack-Modus "Weiter")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS kommission_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                datum          TEXT    NOT NULL,
                produkt_nr     TEXT    NOT NULL,
                produkt_name   TEXT    NOT NULL,
                filiale        TEXT    NOT NULL,
                typ            TEXT    NOT NULL DEFAULT '1.',
                soll           REAL    NOT NULL DEFAULT 0,
                geliefert      REAL    NOT NULL DEFAULT 0,
                nachlege       REAL    NOT NULL DEFAULT 0,
                bestaetigt_um  TEXT    NOT NULL,
                UNIQUE(datum, produkt_nr, filiale, typ)
            )
        """)
        # Mengenänderungen (Kürzung / Vermehrung / Nachlegen)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS korrektur_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                datum          TEXT    NOT NULL,
                zeit           TEXT    NOT NULL,
                ereignis       TEXT    NOT NULL,
                produkt_nr     TEXT    NOT NULL,
                produkt_name   TEXT    NOT NULL,
                filiale        TEXT    NOT NULL,
                typ            TEXT    NOT NULL DEFAULT '1.',
                soll_alt       REAL    NOT NULL DEFAULT 0,
                soll_neu       REAL    NOT NULL DEFAULT 0,
                delta          REAL    NOT NULL DEFAULT 0
            )
        """)
        # Vollständiges Aktions-Log — jede Taste, jeder Klick
        conn.execute("""
            CREATE TABLE IF NOT EXISTS aktions_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                datum       TEXT    NOT NULL,
                zeit        TEXT    NOT NULL,
                aktion      TEXT    NOT NULL,
                details     TEXT    NOT NULL DEFAULT '{}'
            )
        """)
        conn.commit()


def upsert_kommission(
    datum: str,
    produkt_nr: str,
    produkt_name: str,
    filiale: str,
    typ: str,
    soll: float,
    geliefert: float,
    nachlege: float,
    bestaetigt_um: str,
) -> None:
    """
    Schreibt eine Bestätigung in die DB.
    Bei Nachlegen (gleicher Schlüssel) wird UPDATE ausgeführt.
    """
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO kommission_log
                (datum, produkt_nr, produkt_name, filiale, typ,
                 soll, geliefert, nachlege, bestaetigt_um)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(datum, produkt_nr, filiale, typ) DO UPDATE SET
                soll          = excluded.soll,
                geliefert     = excluded.geliefert,
                nachlege      = excluded.nachlege,
                bestaetigt_um = excluded.bestaetigt_um
        """, (datum, produkt_nr, produkt_name, filiale, typ,
              soll, geliefert, nachlege, bestaetigt_um))
        conn.commit()


def log_aktion(aktion: str, details: dict | None = None) -> None:
    """
    Schreibt jede Benutzer-Aktion in aktions_log.
    aktion:  z.B. 'weiter', 'zurueck', 'pdf_geladen', 'kuerzung_angewendet' …
    details: beliebige Zusatzinfos als dict (wird als JSON gespeichert)
    """
    import json as _json
    jetzt = __import__("datetime").datetime.now()
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO aktions_log (datum, zeit, aktion, details)
            VALUES (?, ?, ?, ?)
        """, (
            jetzt.strftime("%Y-%m-%d"),
            jetzt.strftime("%H:%M:%S"),
            aktion,
            _json.dumps(details or {}, ensure_ascii=False),
        ))
        conn.commit()


def get_aktionen_by_datum(datum: str) -> list[dict]:
    """Alle Aktionen für ein Datum, chronologisch."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT id, datum, zeit, aktion, details
            FROM aktions_log
            WHERE datum = ?
            ORDER BY id
        """, (datum,)).fetchall()
    import json as _json
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["details"] = _json.loads(d["details"])
        except Exception:
            pass
        result.append(d)
    return result


def insert_korrektur(
    datum: str,
    zeit: str,
    ereignis: str,
    produkt_nr: str,
    produkt_name: str,
    filiale: str,
    typ: str,
    soll_alt: float,
    soll_neu: float,
) -> None:
    """Schreibt eine Mengenänderung (Kürzung oder Nachlegen) in korrektur_log."""
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO korrektur_log
                (datum, zeit, ereignis, produkt_nr, produkt_name,
                 filiale, typ, soll_alt, soll_neu, delta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (datum, zeit, ereignis, produkt_nr, produkt_name,
              filiale, typ, soll_alt, soll_neu, round(soll_neu - soll_alt, 6)))
        conn.commit()


def get_korrekturen_by_datum(datum: str) -> list[dict]:
    """Alle Korrekturen für ein Datum."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT * FROM korrektur_log
            WHERE datum = ?
            ORDER BY zeit, produkt_nr, filiale
        """, (datum,)).fetchall()
    return [dict(r) for r in rows]


def get_log_by_datum(datum: str) -> list[dict]:
    """Alle Einträge für ein Datum, sortiert nach Produkt und Filiale."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT datum, produkt_nr, produkt_name, filiale, typ,
                   soll, geliefert, nachlege, bestaetigt_um
            FROM kommission_log
            WHERE datum = ?
            ORDER BY produkt_nr, filiale, typ
        """, (datum,)).fetchall()
    return [dict(r) for r in rows]


def get_verfuegbare_daten() -> list[str]:
    """Die letzten 30 Tage mit Einträgen."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT DISTINCT datum
            FROM kommission_log
            ORDER BY datum DESC
            LIMIT 30
        """).fetchall()
    return [r["datum"] for r in rows]


def get_log_summary(datum: str) -> list[dict]:
    """
    Aggregiert pro Produkt + Filiale:
    Gesamt-Soll, Gesamt-Geliefert (1. + V), Nachlege.
    Nützlich für n8n-Export.
    """
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT
                datum,
                produkt_nr,
                produkt_name,
                filiale,
                SUM(soll)      AS soll_gesamt,
                SUM(geliefert) AS geliefert_gesamt,
                SUM(nachlege)  AS nachlege_gesamt,
                MAX(bestaetigt_um) AS letzte_bestaetigung
            FROM kommission_log
            WHERE datum = ?
            GROUP BY datum, produkt_nr, filiale
            ORDER BY produkt_nr, filiale
        """, (datum,)).fetchall()
    return [dict(r) for r in rows]
