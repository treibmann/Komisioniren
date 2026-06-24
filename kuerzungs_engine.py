"""
kuerzungs_engine.py – Mengenkorrektur (Kürzung & Vermehrung)

Logik:
  Für jeden Artikel wird die Ist-Menge (tatsächlich gebacken) gegen die
  Soll-Menge (aus PDF) geprüft. Überschuss oder Mangel werden nach
  Priorität verteilt:

  Priorität 1 – Verkaufsauto:  bekommt immer 100 % (12h unterwegs)
  Priorität 2 – Fremdkunde:    bekommt immer 100 %
  Priorität 3 – Standard V:    Vorbestellungs-Mengen der Standard-Filialen → 100 %
  Priorität 4 – Standard 1.:   Rest wird proportional nach Bestellmenge aufgeteilt

  Gilt für Kürzung UND Vermehrung (gleiche Logik).
"""

import json
import math
import os
from typing import Any

import os as _os
KUERZUNG_CONFIG_FILE = _os.path.join(_os.getenv("DATA_DIR", "data"), "kuerzungs_config.json")
_os.makedirs(_os.path.dirname(KUERZUNG_CONFIG_FILE), exist_ok=True)

# Gültige Filial-Kategorien
KATEGORIEN = ["standard", "verkaufsauto", "fremdkunde"]


# ─────────────────────────────────────────────────────────────────────────────
# Konfig laden / speichern
# ─────────────────────────────────────────────────────────────────────────────

def load_kuerzungs_config() -> dict:
    """
    Lädt die Kürzungs-Konfiguration.
    Struktur:
      {
        "filial_kategorien": { "Penny": "standard", "Verkaufsauto": "verkaufsauto", ... },
        "produkt_rundung":   { "default": 1, "11051": 5, "10102": 0.5, ... }
      }
    """
    if os.path.exists(KUERZUNG_CONFIG_FILE):
        try:
            with open(KUERZUNG_CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            # Defaults sicherstellen
            cfg.setdefault("filial_kategorien", {})
            cfg.setdefault("produkt_rundung", {"default": 1})
            return cfg
        except Exception:
            pass
    return {
        "filial_kategorien": {},
        "produkt_rundung": {"default": 1},
    }


def save_kuerzungs_config(cfg: dict) -> None:
    with open(KUERZUNG_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=4)


# ─────────────────────────────────────────────────────────────────────────────
# Rundungs-Helfer
# ─────────────────────────────────────────────────────────────────────────────

def round_to(wert: float, schritt: float) -> float:
    """Rundet auf das nächste Vielfache von 'schritt'. Beispiel: round_to(3.7, 0.5) → 3.5"""
    if schritt <= 0:
        return wert
    return round(round(wert / schritt) * schritt, 10)


def get_rundung(produkt_nr: str, cfg: dict) -> float:
    """Gibt die Rundungs-Schrittweite für ein Produkt zurück."""
    return float(cfg.get("produkt_rundung", {}).get(produkt_nr,
           cfg.get("produkt_rundung", {}).get("default", 1)))


# ─────────────────────────────────────────────────────────────────────────────
# Kern-Algorithmus
# ─────────────────────────────────────────────────────────────────────────────

def berechne_verteilung(
    produkt_nr: str,
    ist_menge: float,
    soll_erst: dict[str, float],   # {filiale: menge} aus "1." Zeilen
    soll_vor: dict[str, float],    # {filiale: menge} aus "V" Zeilen
    cfg: dict,
) -> dict[str, Any]:
    """
    Berechnet die angepasste Verteilung für einen Artikel.

    Gibt zurück:
      {
        "ist_menge":  float,
        "soll_gesamt": float,
        "differenz":  float,        # + = Vermehrung, - = Kürzung
        "filialen": {
          filiale: {
            "soll_erst": float,
            "soll_vor":  float,
            "neu_erst":  float,     # angepasste 1. Lieferung
            "neu_vor":   float,     # angepasste Vorbestellung
            "kategorie": str,
          }, ...
        }
      }
    """
    filial_kat = cfg.get("filial_kategorien", {})
    rundung = get_rundung(produkt_nr, cfg)

    # Alle Filialen zusammenführen
    alle_filialen = sorted(set(list(soll_erst.keys()) + list(soll_vor.keys())))

    soll_gesamt = sum(soll_erst.get(f, 0) for f in alle_filialen) + \
                  sum(soll_vor.get(f, 0) for f in alle_filialen)

    rest = float(ist_menge)
    result: dict[str, dict] = {
        f: {
            "soll_erst": soll_erst.get(f, 0),
            "soll_vor":  soll_vor.get(f, 0),
            "neu_erst":  0.0,
            "neu_vor":   0.0,
            "kategorie": filial_kat.get(f, "standard"),
        }
        for f in alle_filialen
    }

    # ── Priorität 1: Verkaufsauto (erst + vor, 100 %) ──────────────────────
    for f in alle_filialen:
        if result[f]["kategorie"] == "verkaufsauto":
            bedarf = result[f]["soll_erst"] + result[f]["soll_vor"]
            zuteilen = min(bedarf, rest)
            # Erst Vorbestellung, dann Erst-Lieferung aus Budget
            neu_vor = min(result[f]["soll_vor"], zuteilen)
            neu_erst = min(result[f]["soll_erst"], zuteilen - neu_vor)
            result[f]["neu_vor"] = neu_vor
            result[f]["neu_erst"] = neu_erst
            rest -= (neu_vor + neu_erst)

    # ── Priorität 2: Fremdkunden (erst + vor, 100 %) ───────────────────────
    for f in alle_filialen:
        if result[f]["kategorie"] == "fremdkunde":
            bedarf = result[f]["soll_erst"] + result[f]["soll_vor"]
            zuteilen = min(bedarf, rest)
            neu_vor = min(result[f]["soll_vor"], zuteilen)
            neu_erst = min(result[f]["soll_erst"], zuteilen - neu_vor)
            result[f]["neu_vor"] = neu_vor
            result[f]["neu_erst"] = neu_erst
            rest -= (neu_vor + neu_erst)

    # ── Priorität 3: Standard-Filialen – Vorbestellung (100 %) ─────────────
    for f in alle_filialen:
        if result[f]["kategorie"] == "standard":
            neu_vor = min(result[f]["soll_vor"], rest)
            result[f]["neu_vor"] = neu_vor
            rest -= neu_vor

    # ── Priorität 4: Standard-Filialen – Erstlieferung (proportional) ──────
    standard_erst = {f: result[f]["soll_erst"] for f in alle_filialen
                     if result[f]["kategorie"] == "standard" and result[f]["soll_erst"] > 0}
    total_standard_erst = sum(standard_erst.values())

    if total_standard_erst > 0 and rest > 0:
        rohe_anteile: dict[str, float] = {}
        for f, soll in standard_erst.items():
            anteil = (soll / total_standard_erst) * rest
            rohe_anteile[f] = round_to(anteil, rundung)

        # Rundungs-Korrektur: Summe muss <= rest sein
        summe = sum(rohe_anteile.values())
        diff = round(summe - rest, 6)
        if diff != 0:
            # Korrektur auf die Filiale mit größtem Anteil
            groesste = max(rohe_anteile, key=lambda f: rohe_anteile[f])
            rohe_anteile[groesste] = round_to(rohe_anteile[groesste] - diff, rundung)
            # Sicherheitsnetz: nicht negativ werden
            rohe_anteile[groesste] = max(0.0, rohe_anteile[groesste])

        for f, menge in rohe_anteile.items():
            result[f]["neu_erst"] = menge

    return {
        "ist_menge":   ist_menge,
        "soll_gesamt": soll_gesamt,
        "differenz":   round(ist_menge - soll_gesamt, 6),
        "filialen":    result,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Batch-Berechnung für alle Artikel
# ─────────────────────────────────────────────────────────────────────────────

def berechne_alle(
    df_erst,       # pandas DataFrame, Typ == "1."
    df_vor,        # pandas DataFrame, Typ == "V"
    ist_mengen: dict[str, float],   # {produkt_nr: ist_menge}
    filialen: list[str],
    cfg: dict,
) -> dict[str, Any]:
    """
    Berechnet Kürzung/Vermehrung für alle Artikel die eine Ist-Menge haben.
    Gibt ein dict {produkt_nr: ergebnis} zurück.
    """
    ergebnisse = {}

    # Alle Artikel-Nummern mit Ist-Menge
    for nr, ist in ist_mengen.items():
        if ist <= 0:
            continue

        # Soll-Mengen aus DataFrames lesen
        soll_erst: dict[str, float] = {}
        soll_vor:  dict[str, float] = {}

        if df_erst is not None and not df_erst.empty:
            rows_erst = df_erst[df_erst["Nr"] == nr]
            for _, row in rows_erst.iterrows():
                for f in filialen:
                    if f in row and row[f] > 0:
                        soll_erst[f] = soll_erst.get(f, 0) + float(row[f])

        if df_vor is not None and not df_vor.empty:
            rows_vor = df_vor[df_vor["Nr"] == nr]
            for _, row in rows_vor.iterrows():
                for f in filialen:
                    if f in row and row[f] > 0:
                        soll_vor[f] = soll_vor.get(f, 0) + float(row[f])

        if not soll_erst and not soll_vor:
            continue

        ergebnisse[nr] = berechne_verteilung(nr, ist, soll_erst, soll_vor, cfg)

    return ergebnisse
