#!/usr/bin/env python3
"""
TowerScan Mk2 — scan.py
═══════════════════════
GSM (2G) + LTE (4G) passive cell tower scanner.

Backends
────────
  rtl_power        — RF carrier detection for all bands
  gr-gsm           — GSM BCCH decode → MCC/MNC/LAC/CI  (if installed)
  LTE-Cell-Scanner — LTE MIB decode  → EARFCN/PCI      (if installed)

Tower identity
──────────────
  1. gr-gsm / LTE-Cell-Scanner decode (most accurate)
  2. OpenCelliD CSV offline lookup    (fast, no internet)
  3. OpenCelliD API / Mozilla         (online fallback)

Usage
─────
  python3 scan.py --diagnose
  python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378
  python3 scan.py --scan --lat 50.0759 --lon 14.4378
  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode gsm
  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode lte
  python3 scan.py --stats
"""

import subprocess
import sqlite3
import csv
import gzip
import json
import math
import os
import re
import argparse
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH     = Path(__file__).parent / "towers.db"
CONFIG_PATH = Path(__file__).parent / "config.json"
OCID_PATH   = Path(__file__).parent / "cell_towers.csv.gz"

# ── Band definitions ──────────────────────────────────────────────────────────

GSM_BANDS = {
    "GSM-900":  (935_000_000,   960_000_000),
    "GSM-1800": (1_805_000_000, 1_880_000_000),
}

# LTE downlink EARFCN band centres for Czech Republic operators
# Full list: https://www.sqimway.com/lte_band.php
LTE_BANDS = {
    "LTE-800":  (791_000_000,   821_000_000,  20),  # Band 20 — 800 MHz
    "LTE-1800": (1_805_000_000, 1_880_000_000, 3),  # Band 3  — 1800 MHz
    "LTE-2100": (2_110_000_000, 2_170_000_000, 1),  # Band 1  — 2100 MHz
    "LTE-2600": (2_620_000_000, 2_690_000_000, 7),  # Band 7  — 2600 MHz
}

# EARFCN → frequency conversion per band
LTE_BAND_PARAMS = {
    1:  {"dl_low": 2110.0, "earfcn_offset": 0,    "earfcn_range": (0,    599)},
    3:  {"dl_low": 1805.0, "earfcn_offset": 1200,  "earfcn_range": (1200, 1949)},
    7:  {"dl_low": 2620.0, "earfcn_offset": 2750,  "earfcn_range": (2750, 3449)},
    20: {"dl_low": 791.0,  "earfcn_offset": 6150,  "earfcn_range": (6150, 6449)},
}

BIN_SIZE_GSM = 100_000   # 100 kHz — resolves 200 kHz GSM channels
BIN_SIZE_LTE = 200_000   # 200 kHz — LTE channel spacing is 1.4–20 MHz

# ── Config ────────────────────────────────────────────────────────────────────

def load_config():
    defaults = {
        "lat": None, "lon": None,
        "opencellid_token": "",
        "scan_duration": 60,
        "gsm_bands":  ["GSM-900", "GSM-1800"],
        "lte_bands":  ["LTE-800", "LTE-1800", "LTE-2100"],
        "ppm": 0, "gain": 40,
        "gsm_threshold": 12.0,
        "lte_threshold": 8.0,    # LTE OFDM signals look wider/flatter
    }
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            return {**defaults, **json.load(f)}
    return defaults

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

# ── Database ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS towers (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            radio        TEXT DEFAULT 'GSM',
            mcc          INTEGER,
            mnc          INTEGER,
            lac          INTEGER,
            cell_id      INTEGER,
            arfcn        INTEGER,
            pci          INTEGER,
            freq_mhz     REAL,
            band         TEXT,
            signal_dbm   REAL,
            lat          REAL,
            lon          REAL,
            range_m      INTEGER DEFAULT 0,
            observer_lat REAL,
            observer_lon REAL,
            operator     TEXT,
            country      TEXT,
            first_seen   TEXT,
            last_seen    TEXT,
            seen_count   INTEGER DEFAULT 1,
            UNIQUE(mcc, mnc, lac, cell_id)
        );

        CREATE TABLE IF NOT EXISTS detections (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            radio        TEXT DEFAULT 'GSM',
            arfcn        INTEGER,
            pci          INTEGER,
            freq_mhz     REAL,
            band         TEXT,
            signal_dbm   REAL,
            noise_floor  REAL,
            observer_lat REAL,
            observer_lon REAL,
            timestamp    TEXT,
            UNIQUE(arfcn, band)
        );

        CREATE TABLE IF NOT EXISTS observations (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            radio        TEXT DEFAULT 'GSM',
            mcc          INTEGER,
            mnc          INTEGER,
            lac          INTEGER,
            cell_id      INTEGER,
            arfcn        INTEGER,
            pci          INTEGER,
            signal_dbm   REAL,
            observer_lat REAL,
            observer_lon REAL,
            timestamp    TEXT
        );
    """)
    conn.commit()

    # Migrations — safe to run on old databases
    migrations = [
        "ALTER TABLE towers     ADD COLUMN radio    TEXT DEFAULT 'GSM'",
        "ALTER TABLE towers     ADD COLUMN pci      INTEGER",
        "ALTER TABLE towers     ADD COLUMN range_m  INTEGER DEFAULT 0",
        "ALTER TABLE detections ADD COLUMN radio    TEXT DEFAULT 'GSM'",
        "ALTER TABLE detections ADD COLUMN pci      INTEGER",
        "ALTER TABLE observations ADD COLUMN radio  TEXT DEFAULT 'GSM'",
        "ALTER TABLE observations ADD COLUMN pci    INTEGER",
    ]
    for sql in migrations:
        try:
            conn.execute(sql); conn.commit()
        except Exception:
            pass

    conn.close()

# ── ARFCN / EARFCN conversions ────────────────────────────────────────────────

def freq_to_gsm_arfcn(freq_hz, band):
    f = freq_hz / 1e6
    if band == "GSM-900":
        a = round((f - 935.0) / 0.2) + 1
        if 1 <= a <= 124:    return a
        a = round((f - 935.0) / 0.2) + 1024
        if 975 <= a <= 1023: return a
    elif band == "GSM-1800":
        a = round((f - 1805.2) / 0.2) + 512
        if 512 <= a <= 885:  return a
    return None

def gsm_arfcn_to_freq(arfcn, band):
    if band == "GSM-900":
        if 1 <= arfcn <= 124:    return round(935.0 + 0.2*(arfcn-1),    1)
        if 975 <= arfcn <= 1023: return round(935.0 + 0.2*(arfcn-1024), 1)
    elif band == "GSM-1800":
        if 512 <= arfcn <= 885:  return round(1805.2 + 0.2*(arfcn-512), 1)
    return None

def freq_to_earfcn(freq_hz, lte_band_num):
    params = LTE_BAND_PARAMS.get(lte_band_num)
    if not params:
        return None
    f_mhz = freq_hz / 1e6
    earfcn = round((f_mhz - params["dl_low"]) / 0.1) + params["earfcn_offset"]
    lo, hi = params["earfcn_range"]
    return earfcn if lo <= earfcn <= hi else None

def earfcn_to_freq(earfcn, lte_band_num):
    params = LTE_BAND_PARAMS.get(lte_band_num)
    if not params:
        return None
    return round(params["dl_low"] + 0.1 * (earfcn - params["earfcn_offset"]), 1)

# ── rtl_power ─────────────────────────────────────────────────────────────────

def run_rtl_power(freq_start, freq_end, bin_hz, gain, ppm, duration_sec):
    cmd = [
        "rtl_power",
        "-f", f"{freq_start}:{freq_end}:{bin_hz}",
        "-g", str(gain),
        "-p", str(ppm),
        "-i", "5",
        "-e", str(duration_sec),
        "-",
    ]
    rows = []
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=duration_sec + 30)
        for line in r.stdout.splitlines():
            try:
                p = line.strip().split(", ")
                if len(p) < 7:
                    continue
                rows.append({
                    "freq_low":  float(p[2]),
                    "freq_high": float(p[3]),
                    "bin_size":  float(p[4]),
                    "powers":    [float(x) for x in p[6:]],
                })
            except (ValueError, IndexError):
                continue
    except subprocess.TimeoutExpired:
        print("    rtl_power timed out")
    except FileNotFoundError:
        print("    rtl_power not found")
    except Exception as e:
        print(f"    rtl_power error: {e}")
    return rows

def find_carriers(rows, threshold_db=12.0):
    freq_powers = {}
    for row in rows:
        for i, pwr in enumerate(row["powers"]):
            freq = row["freq_low"] + row["bin_size"] * i
            freq_powers.setdefault(freq, []).append(pwr)

    if not freq_powers:
        return []

    avg    = {f: sum(v)/len(v) for f, v in freq_powers.items()}
    vals   = sorted(avg.values())
    noise  = vals[len(vals) // 2]
    peaks  = {f: p for f, p in avg.items() if p - noise >= threshold_db}
    return avg, noise, peaks

# ── gr-gsm decoder ────────────────────────────────────────────────────────────

def decode_gsm_channel(freq_hz, gain, ppm, duration=20):
    """
    Run grgsm_livemon_headless and capture UDP output via tshark.
    Returns list of decoded cell dicts with mcc/mnc/lac/cell_id.
    """
    cells = {}

    # Check tool available
    if not _tool_exists("grgsm_livemon_headless"):
        return []

    # Start grgsm in background
    gsm_cmd = [
        "grgsm_livemon_headless",
        f"--fc={int(freq_hz)}",
        "--samp-rate=2000000",
        f"--gain={gain}",
        f"--ppm={ppm}",
    ]

    tshark_cmd = [
        "tshark", "-i", "lo", "-f", "udp",
        "-Y", "gsm_a.rr",
        "-T", "fields",
        "-e", "gsm_a.rr.mobile_country_code",
        "-e", "gsm_a.rr.mobile_network_code",
        "-e", "gsm_a.rr.lac",
        "-e", "gsm_a.rr.cell_id",
        "-l",
    ]

    try:
        gsm_proc    = subprocess.Popen(gsm_cmd,    stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL)
        tshark_proc = subprocess.Popen(tshark_cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, text=True)

        deadline = time.time() + duration
        while time.time() < deadline:
            line = tshark_proc.stdout.readline()
            if not line:
                time.sleep(0.1)
                continue
            parts = line.strip().split("\t")
            if len(parts) == 4 and all(parts):
                try:
                    mcc, mnc, lac, ci = [int(x) for x in parts]
                    key = (mcc, mnc, lac, ci)
                    if key not in cells:
                        cells[key] = {"mcc": mcc, "mnc": mnc,
                                      "lac": lac, "cell_id": ci}
                except ValueError:
                    continue
    except Exception as e:
        print(f"    gr-gsm decode error: {e}")
    finally:
        for p in [gsm_proc, tshark_proc]:
            try: p.terminate(); p.wait(timeout=3)
            except Exception: pass

    return list(cells.values())

# ── LTE-Cell-Scanner ──────────────────────────────────────────────────────────

def scan_lte_band(freq_start, freq_end, gain, ppm, duration=30):
    """
    Run LTE-Cell-Scanner across a frequency range.
    Returns list of dicts: {earfcn, pci, freq_mhz, band_num, rsrp}
    """
    scanner = _find_lte_scanner()
    if not scanner:
        return []

    cells = []
    # LTE-Cell-Scanner works best scanning individual centre frequencies
    # Step through the band in 5 MHz steps (LTE channel BW)
    step = 5_000_000
    freqs = list(range(freq_start, freq_end, step))

    for fc in freqs:
        cmd = [
            scanner,
            "--freq-start", str(fc),
            "--freq-end",   str(fc + step),
            "--gain",       str(gain * 10),  # LTE-Cell-Scanner uses 10x gain units
            "--ppm",        str(ppm),
            "--time",       str(min(duration // max(len(freqs), 1), 10)),
        ]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=duration + 10)
            cells.extend(_parse_lte_scanner_output(r.stdout + r.stderr))
        except subprocess.TimeoutExpired:
            pass
        except Exception as e:
            print(f"    LTE scan error at {fc/1e6:.0f} MHz: {e}")

    # Deduplicate by PCI
    seen = {}
    for c in cells:
        k = c["pci"]
        if k not in seen or c.get("rsrp", -999) > seen[k].get("rsrp", -999):
            seen[k] = c

    return list(seen.values())

def _parse_lte_scanner_output(text):
    """Parse LTE-Cell-Scanner stdout for cell info."""
    cells = []
    # LTE-Cell-Scanner output format:
    # Detected a cell! EARFCN: 1300 RxLevel: -10.2 dB CellID: 42 ...
    earfcn_re = re.compile(r"EARFCN[:\s]+(\d+)", re.I)
    pci_re    = re.compile(r"(?:Cell.?ID|PCI)[:\s]+(\d+)", re.I)
    rsrp_re   = re.compile(r"(?:RxLevel|RSRP)[:\s]+([-\d.]+)", re.I)

    current = {}
    for line in text.splitlines():
        if "cell" in line.lower() and ("earfcn" in line.lower() or "detect" in line.lower()):
            current = {}
        if m := earfcn_re.search(line): current["earfcn"] = int(m.group(1))
        if m := pci_re.search(line):    current["pci"]    = int(m.group(1))
        if m := rsrp_re.search(line):   current["rsrp"]   = float(m.group(1))

        if "earfcn" in current and "pci" in current:
            earfcn = current["earfcn"]
            # Map EARFCN to frequency and band
            for band_num, params in LTE_BAND_PARAMS.items():
                lo, hi = params["earfcn_range"]
                if lo <= earfcn <= hi:
                    freq_mhz = earfcn_to_freq(earfcn, band_num)
                    if freq_mhz:
                        cells.append({
                            "earfcn":   earfcn,
                            "pci":      current["pci"],
                            "freq_mhz": freq_mhz,
                            "band_num": band_num,
                            "rsrp":     current.get("rsrp", -90),
                            "radio":    "LTE",
                        })
                    break
            current = {}

    return cells

def _find_lte_scanner():
    for name in ["LTE-Cell-Scanner", "CellSearch", "lte-cell-scanner"]:
        r = subprocess.run(["which", name], capture_output=True, text=True)
        if r.returncode == 0:
            return r.stdout.strip()
    return None

def _tool_exists(name):
    return subprocess.run(["which", name], capture_output=True).returncode == 0

# ── OpenCelliD CSV ────────────────────────────────────────────────────────────

def load_ocid_csv():
    path = OCID_PATH
    if not path.exists():
        path = path.with_suffix("")   # try uncompressed
    if not path.exists():
        return []

    opener = gzip.open if str(path).endswith(".gz") else open
    towers = []
    try:
        with opener(path, "rt", encoding="utf-8", errors="ignore") as f:
            for row in csv.DictReader(f):
                radio = row.get("radio", "").upper()
                if radio not in ("GSM", "LTE", "UMTS"):
                    continue
                try:
                    towers.append({
                        "radio":   radio,
                        "mcc":     int(row["mcc"]),
                        "mnc":     int(row["net"]),
                        "lac":     int(row["area"]),
                        "cell_id": int(row["cell"]),
                        "lon":     float(row["lon"]),
                        "lat":     float(row["lat"]),
                        "range_m": int(row.get("range", 0) or 0),
                    })
                except (ValueError, KeyError):
                    continue
    except Exception as e:
        print(f"  OCID CSV error: {e}")
    return towers

def haversine_m(a, b):
    dlat = math.radians(b[0] - a[0])
    dlon = math.radians(b[1] - a[1])
    x = (math.sin(dlat/2)**2 +
         math.cos(math.radians(a[0])) * math.cos(math.radians(b[0])) *
         math.sin(dlon/2)**2)
    return round(6_371_000 * 2 * math.asin(math.sqrt(x)))

def nearby_towers(ocid, lat, lon, radius_m=15_000, radio=None):
    results = []
    for t in ocid:
        if radio and t["radio"] != radio:
            continue
        d = haversine_m((lat, lon), (t["lat"], t["lon"]))
        if d <= radius_m:
            results.append({**t, "dist_m": d})
    return sorted(results, key=lambda x: x["dist_m"])

# ── Operator table ────────────────────────────────────────────────────────────

OPERATORS = {
    (230, 1):  ("T-Mobile CZ",    "Czech Republic"),
    (230, 2):  ("O2 CZ",          "Czech Republic"),
    (230, 3):  ("Vodafone CZ",    "Czech Republic"),
    (230, 4):  ("Nordic Telecom", "Czech Republic"),
    (230, 6):  ("Sazka Mobil",    "Czech Republic"),
    (230, 98): ("Eltodo",         "Czech Republic"),
    (262, 1):  ("T-Mobile DE",    "Germany"),
    (262, 2):  ("Vodafone DE",    "Germany"),
    (262, 7):  ("O2 DE",          "Germany"),
    (234, 10): ("O2 UK",          "United Kingdom"),
    (234, 20): ("3 UK",           "United Kingdom"),
    (234, 30): ("EE UK",          "United Kingdom"),
    (208, 1):  ("Orange FR",      "France"),
    (208, 10): ("SFR",            "France"),
    (208, 20): ("Bouygues",       "France"),
}

def get_operator(mcc, mnc):
    return OPERATORS.get((mcc, mnc), (f"MCC{mcc}/MNC{mnc}", "Unknown"))

# ── Online lookups ────────────────────────────────────────────────────────────

def lookup_opencellid(mcc, mnc, lac, cell_id, token):
    if not token or not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        import requests as req
        r = req.get("https://opencellid.org/cell/get", timeout=5, params={
            "key": token, "mcc": mcc, "mnc": mnc,
            "lac": lac, "cellid": cell_id, "format": "json"})
        if r.status_code == 200:
            d = r.json()
            if "lat" in d:
                return float(d["lat"]), float(d["lon"]), int(d.get("range", 0))
    except Exception:
        pass
    return None

def lookup_mozilla(mcc, mnc, lac, cell_id):
    if not all([mcc, mnc, lac, cell_id]):
        return None
    try:
        import requests as req
        r = req.post(
            "https://location.services.mozilla.com/v1/geolocate?key=test",
            json={"cellTowers": [{"radioType": "gsm",
                "mobileCountryCode": mcc, "mobileNetworkCode": mnc,
                "locationAreaCode": lac, "cellId": cell_id}]},
            timeout=5)
        if r.status_code == 200:
            loc = r.json().get("location", {})
            if "lat" in loc:
                return float(loc["lat"]), float(loc["lng"]), 0
    except Exception:
        pass
    return None

# ── Store results ─────────────────────────────────────────────────────────────

def now_iso():
    return datetime.now(timezone.utc).isoformat()

def store_detection(radio, arfcn, pci, freq_mhz, band, signal_dbm,
                    noise_floor, obs_lat, obs_lon):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        INSERT INTO detections
            (radio, arfcn, pci, freq_mhz, band, signal_dbm,
             noise_floor, observer_lat, observer_lon, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(arfcn, band) DO UPDATE SET
            signal_dbm = excluded.signal_dbm,
            pci        = COALESCE(excluded.pci, detections.pci),
            timestamp  = excluded.timestamp
    """, (radio, arfcn, pci, freq_mhz, band, signal_dbm,
          noise_floor, obs_lat, obs_lon, now_iso()))
    conn.commit()
    conn.close()

def store_tower(radio, mcc, mnc, lac, cell_id, arfcn, pci,
                freq_mhz, band, signal_dbm, lat, lon, range_m,
                obs_lat, obs_lon, config):
    conn = sqlite3.connect(DB_PATH)
    n = now_iso()

    # Try to get coords if missing
    if not lat:
        c = lookup_opencellid(mcc, mnc, lac, cell_id,
                              config.get("opencellid_token", ""))
        if not c:
            c = lookup_mozilla(mcc, mnc, lac, cell_id)
        if c:
            lat, lon, range_m = c

    operator, country = get_operator(mcc, mnc)

    conn.execute("""
        INSERT INTO towers
            (radio, mcc, mnc, lac, cell_id, arfcn, pci, freq_mhz, band,
             signal_dbm, lat, lon, range_m, observer_lat, observer_lon,
             operator, country, first_seen, last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(mcc, mnc, lac, cell_id) DO UPDATE SET
            last_seen  = excluded.last_seen,
            seen_count = seen_count + 1,
            signal_dbm = excluded.signal_dbm,
            pci        = COALESCE(excluded.pci, towers.pci),
            lat        = COALESCE(excluded.lat, towers.lat),
            lon        = COALESCE(excluded.lon, towers.lon)
    """, (radio, mcc, mnc, lac, cell_id, arfcn, pci, freq_mhz, band,
          signal_dbm, lat, lon, range_m, obs_lat, obs_lon,
          operator, country, n, n))

    conn.execute("""
        INSERT INTO observations
            (radio, mcc, mnc, lac, cell_id, arfcn, pci,
             signal_dbm, observer_lat, observer_lon, timestamp)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (radio, mcc, mnc, lac, cell_id, arfcn, pci,
          signal_dbm, obs_lat, obs_lon, n))

    conn.commit()
    conn.close()

# ── GSM scan ──────────────────────────────────────────────────────────────────

def run_gsm_scan(config, ocid, bands=None):
    bands    = bands or config.get("gsm_bands", ["GSM-900", "GSM-1800"])
    obs_lat  = config.get("lat")
    obs_lon  = config.get("lon")
    duration = config.get("scan_duration", 60)
    gain     = config.get("gain", 40)
    ppm      = config.get("ppm", 0)
    thresh   = config.get("gsm_threshold", 12.0)
    has_grgsm = _tool_exists("grgsm_livemon_headless") and _tool_exists("tshark")

    total_c = total_t = 0

    for band in bands:
        freq_start, freq_end = GSM_BANDS[band]
        print(f"\n  [{band}] {freq_start/1e6:.0f}–{freq_end/1e6:.0f} MHz | "
              f"{duration}s | gain={gain} ppm={ppm}")

        rows = run_rtl_power(freq_start, freq_end, BIN_SIZE_GSM,
                              gain, ppm, duration)
        if not rows:
            print("    No rtl_power data.")
            continue

        result = find_carriers(rows, threshold_db=thresh)
        if not result:
            print(f"    No carriers above noise+{thresh}dB.")
            continue

        avg, noise, peaks = result
        print(f"    Noise floor: {noise:.1f} dBm | "
              f"{len(peaks)} carrier(s) detected")

        for freq_hz, pwr in sorted(peaks.items(),
                                    key=lambda x: x[1], reverse=True):
            arfcn = freq_to_gsm_arfcn(freq_hz, band)
            if not arfcn:
                continue
            freq_mhz = gsm_arfcn_to_freq(arfcn, band) or round(freq_hz/1e6, 1)
            above = pwr - noise
            print(f"    ARFCN {arfcn:4d}  {freq_mhz:.1f} MHz  "
                  f"{pwr:.1f} dBm  (+{above:.1f} dB)")

            store_detection("GSM", arfcn, None, freq_mhz, band,
                            round(pwr, 1), round(noise, 1),
                            obs_lat, obs_lon)
            total_c += 1

            # Try gr-gsm decode first
            decoded = []
            if has_grgsm:
                print(f"      → Decoding with gr-gsm ({20}s)...")
                decoded = decode_gsm_channel(freq_hz, gain, ppm, duration=20)
                if decoded:
                    print(f"      → Decoded {len(decoded)} cell(s) via gr-gsm")

            # OCID proximity fallback
            if not decoded and ocid and obs_lat and obs_lon:
                nearby = nearby_towers(ocid, obs_lat, obs_lon,
                                       radius_m=15_000, radio="GSM")
                decoded = [{"mcc": t["mcc"], "mnc": t["mnc"],
                            "lac": t["lac"], "cell_id": t["cell_id"],
                            "lat": t["lat"], "lon": t["lon"],
                            "range_m": t["range_m"],
                            "_source": "ocid_proximity"}
                           for t in nearby[:5]]
                if decoded:
                    print(f"      → {len(decoded)} OCID proximity match(es)")

            for cell in decoded:
                op, country = get_operator(cell["mcc"], cell["mnc"])
                source = cell.get("_source", "gr-gsm")
                store_tower(
                    "GSM", cell["mcc"], cell["mnc"],
                    cell["lac"], cell["cell_id"],
                    arfcn, None, freq_mhz, band,
                    round(pwr, 1),
                    cell.get("lat"), cell.get("lon"),
                    cell.get("range_m", 0),
                    obs_lat, obs_lon, config)
                print(f"      ✓ {op} | LAC={cell['lac']} CI={cell['cell_id']} "
                      f"[{source}]")
                total_t += 1

    return total_c, total_t

# ── LTE scan ──────────────────────────────────────────────────────────────────

def run_lte_scan(config, ocid, bands=None):
    bands    = bands or config.get("lte_bands", ["LTE-800", "LTE-1800", "LTE-2100"])
    obs_lat  = config.get("lat")
    obs_lon  = config.get("lon")
    duration = config.get("scan_duration", 60)
    gain     = config.get("gain", 40)
    ppm      = config.get("ppm", 0)
    thresh   = config.get("lte_threshold", 8.0)
    has_lte  = bool(_find_lte_scanner())

    total_c = total_t = 0

    for band in bands:
        if band not in LTE_BANDS:
            print(f"\n  [{band}] Unknown band, skipping.")
            continue

        freq_start, freq_end, band_num = LTE_BANDS[band]
        print(f"\n  [{band}] {freq_start/1e6:.0f}–{freq_end/1e6:.0f} MHz | "
              f"{duration}s | gain={gain} ppm={ppm}")

        # Step 1: rtl_power to find candidate frequencies
        rows = run_rtl_power(freq_start, freq_end, BIN_SIZE_LTE,
                              gain, ppm, duration)
        if not rows:
            print("    No rtl_power data.")
            continue

        result = find_carriers(rows, threshold_db=thresh)
        if not result:
            print(f"    No LTE carriers above noise+{thresh}dB.")
            print(f"    (LTE signals are wide and flat — try --lte-threshold 5)")
            continue

        avg, noise, peaks = result
        print(f"    Noise floor: {noise:.1f} dBm | "
              f"{len(peaks)} candidate(s)")

        # Step 2: LTE-Cell-Scanner on candidate frequencies
        lte_cells = []
        if has_lte and peaks:
            print(f"    Running LTE-Cell-Scanner...")
            # Scan around each peak ± 5 MHz
            for freq_hz in sorted(peaks.keys(),
                                   key=lambda f: peaks[f], reverse=True)[:5]:
                fc_start = max(freq_start, int(freq_hz) - 5_000_000)
                fc_end   = min(freq_end,   int(freq_hz) + 5_000_000)
                cells = scan_lte_band(fc_start, fc_end, gain, ppm,
                                      duration=min(30, duration//2))
                lte_cells.extend(cells)

        # Step 3: Record detections and identify towers
        for freq_hz, pwr in sorted(peaks.items(),
                                    key=lambda x: x[1], reverse=True):
            earfcn = freq_to_earfcn(freq_hz, band_num)
            freq_mhz = round(freq_hz / 1e6, 1)
            above = pwr - noise
            print(f"    EARFCN {earfcn or '?':>6}  {freq_mhz:.1f} MHz  "
                  f"{pwr:.1f} dBm  (+{above:.1f} dB)")

            # Find matching LTE-Cell-Scanner result
            matched_pci = None
            for c in lte_cells:
                if abs(c["freq_mhz"] - freq_mhz) < 2.5:
                    matched_pci = c["pci"]
                    break

            store_detection("LTE", earfcn, matched_pci, freq_mhz, band,
                            round(pwr, 1), round(noise, 1),
                            obs_lat, obs_lon)
            total_c += 1

            if matched_pci is not None:
                print(f"      → PCI={matched_pci} (from LTE-Cell-Scanner)")

            # OCID LTE lookup
            if ocid and obs_lat and obs_lon:
                nearby = nearby_towers(ocid, obs_lat, obs_lon,
                                       radius_m=15_000, radio="LTE")
                if nearby:
                    print(f"      → {len(nearby)} LTE tower(s) in OCID within 15km")
                    for t in nearby[:3]:
                        op, country = get_operator(t["mcc"], t["mnc"])
                        store_tower(
                            "LTE", t["mcc"], t["mnc"],
                            t["lac"], t["cell_id"],
                            earfcn, matched_pci, freq_mhz, band,
                            round(pwr, 1),
                            t["lat"], t["lon"], t["range_m"],
                            obs_lat, obs_lon, config)
                        print(f"        ✓ {op} | CI={t['cell_id']} "
                              f"dist={t['dist_m']}m [ocid]")
                        total_t += 1

    return total_c, total_t

# ── OCID import ───────────────────────────────────────────────────────────────

def import_ocid(config, radius_km=15):
    obs_lat = config.get("lat")
    obs_lon = config.get("lon")
    if not obs_lat or not obs_lon:
        print("ERROR: --lat and --lon required.")
        return

    print(f"Loading OCID database...")
    ocid = load_ocid_csv()
    if not ocid:
        print("No OCID CSV found. Download it first — see README.")
        return

    print(f"Loaded {len(ocid):,} records.")
    radius_m = radius_km * 1000

    conn = sqlite3.connect(DB_PATH)
    n = now_iso()
    inserted = updated = 0

    for radio in ("GSM", "LTE", "UMTS"):
        towers = nearby_towers(ocid, obs_lat, obs_lon, radius_m, radio=radio)
        print(f"  {radio}: {len(towers)} towers within {radius_km}km")
        for t in towers:
            op, country = get_operator(t["mcc"], t["mnc"])
            try:
                cur = conn.execute(
                    "SELECT id FROM towers WHERE mcc=? AND mnc=? AND lac=? AND cell_id=?",
                    (t["mcc"], t["mnc"], t["lac"], t["cell_id"])).fetchone()
                if cur:
                    conn.execute("""
                        UPDATE towers SET lat=COALESCE(lat,?), lon=COALESCE(lon,?),
                            range_m=COALESCE(range_m,?), last_seen=?
                        WHERE mcc=? AND mnc=? AND lac=? AND cell_id=?
                    """, (t["lat"], t["lon"], t["range_m"], n,
                          t["mcc"], t["mnc"], t["lac"], t["cell_id"]))
                    updated += 1
                else:
                    conn.execute("""
                        INSERT INTO towers
                            (radio, mcc, mnc, lac, cell_id, lat, lon, range_m,
                             operator, country, first_seen, last_seen)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    """, (radio, t["mcc"], t["mnc"], t["lac"], t["cell_id"],
                          t["lat"], t["lon"], t["range_m"],
                          op, country, n, n))
                    inserted += 1
            except Exception:
                continue

    conn.commit()
    conn.close()
    print(f"\nImported {inserted} new towers, updated {inserted+updated} total.")
    show_stats()

# ── Full scan ─────────────────────────────────────────────────────────────────

def run_scan(config, mode="all", gsm_bands=None, lte_bands=None):
    obs_lat = config.get("lat")
    obs_lon = config.get("lon")
    if not obs_lat or not obs_lon:
        print("WARNING: No observer coordinates. Use --lat and --lon.\n")

    print("Loading OCID database...")
    ocid = load_ocid_csv()
    if ocid:
        print(f"  {len(ocid):,} records loaded.")
    else:
        print("  No OCID CSV found — using online lookup only.")

    total_c = total_t = 0

    if mode in ("all", "gsm"):
        print("\n" + "═"*50)
        print("  GSM SCAN")
        print("═"*50)
        c, t = run_gsm_scan(config, ocid, bands=gsm_bands)
        total_c += c; total_t += t

    if mode in ("all", "lte"):
        print("\n" + "═"*50)
        print("  LTE SCAN")
        print("═"*50)
        if not _find_lte_scanner():
            print("\n  LTE-Cell-Scanner not installed.")
            print("  Run setup.sh to install it, or LTE carriers will be")
            print("  detected by rtl_power only (no PCI/EARFCN decode).\n")
        c, t = run_lte_scan(config, ocid, bands=lte_bands)
        total_c += c; total_t += t

    print("\n" + "═"*50)
    print(f"  Scan complete | {total_c} carriers | {total_t} towers")
    print("═"*50)
    show_stats()

# ── Stats ─────────────────────────────────────────────────────────────────────

def show_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM towers");                    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL"); mapped = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM detections");                dets  = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM observations");              obs   = c.fetchone()[0]
    c.execute("SELECT radio, COUNT(*) FROM towers GROUP BY radio"); by_radio = c.fetchall()
    c.execute("SELECT operator, COUNT(*) n FROM towers GROUP BY operator ORDER BY n DESC LIMIT 8")
    ops = c.fetchall()
    conn.close()

    print(f"\n{'═'*44}")
    print(f" Database")
    print(f"{'─'*44}")
    print(f" Towers (identified) : {total} ({mapped} with GPS)")
    for radio, n in by_radio:
        print(f"   {radio:<6} : {n}")
    print(f" Raw detections      : {dets}")
    print(f" Observations        : {obs}")
    if ops:
        print(f"\n Top operators:")
        for op, n in ops:
            print(f"   {op:<28} {n}")
    print(f"{'═'*44}")

# ── Diagnostic ────────────────────────────────────────────────────────────────

def diagnose():
    print("═"*44)
    print(" TowerScan Mk2 — Diagnostic")
    print("═"*44)

    def check(label, cmd, ok_str=None, timeout=5):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            out = r.stdout + r.stderr
            found = ok_str is None or ok_str.lower() in out.lower()
            status = "✓" if found else "✗"
            print(f"  {status} {label}")
            if found and ok_str:
                for line in out.splitlines():
                    if ok_str.lower() in line.lower():
                        print(f"      {line.strip()}")
            return found
        except subprocess.TimeoutExpired:
            print(f"  ? {label} (timeout)")
        except FileNotFoundError:
            print(f"  ✗ {label} (not found)")
        return False

    print("\n[Hardware]")
    RTL = subprocess.run(["timeout", "4", "rtl_test"],
                          capture_output=True, text=True)
    out = RTL.stdout + RTL.stderr
    for kw in ["Found", "Tuner", "E4000", "R820"]:
        for line in out.splitlines():
            if kw.lower() in line.lower():
                print(f"  ✓ {line.strip()}")
                break

    print("\n[Tools]")
    check("rtl_power",            ["which", "rtl_power"])
    check("rtl_sdr",              ["which", "rtl_sdr"])
    check("kal (kalibrate-rtl)",  ["which", "kal"])
    check("grgsm_scanner",        ["which", "grgsm_scanner"])
    check("grgsm_livemon_headless",["which", "grgsm_livemon_headless"])
    check("tshark",               ["which", "tshark"])
    lte = _find_lte_scanner()
    print(f"  {'✓' if lte else '✗'} LTE-Cell-Scanner{f' → {lte}' if lte else ' (not found)'}")

    print("\n[rtl_power quick test — 950 MHz, 10s]")
    r = subprocess.run(
        ["rtl_power", "-f", "949M:951M:100k", "-g", "40", "-i", "5", "-e", "10", "-"],
        capture_output=True, text=True, timeout=20)
    lines = [l for l in r.stdout.splitlines() if l.strip()]
    if lines:
        try:
            vals = [float(x) for x in lines[0].split(", ")[6:]]
            print(f"  ✓ {len(lines)} sweep(s) | "
                  f"peak={max(vals):.1f} noise={min(vals):.1f} "
                  f"Δ={max(vals)-min(vals):.1f} dB")
        except Exception:
            print(f"  ✓ {len(lines)} sweep(s)")
    else:
        print(f"  ✗ No output")

    print("\n[OCID CSV]")
    p = OCID_PATH if OCID_PATH.exists() else OCID_PATH.with_suffix("")
    if p.exists():
        print(f"  ✓ {p} ({p.stat().st_size/1e6:.1f} MB)")
    else:
        print(f"  ✗ Not found at {OCID_PATH}")
        print(f"      wget -O cell_towers.csv.gz \\")
        print(f"        'https://opencellid.org/ocid/downloads?token=TOKEN&type=mcc&file=mcc-230.csv.gz'")

    print("\n" + "═"*44)

# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="TowerScan Mk2 — GSM + LTE cell tower scanner",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 scan.py --diagnose
  python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378
  python3 scan.py --scan --lat 50.0759 --lon 14.4378
  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode gsm
  python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode lte
  python3 scan.py --scan --gsm-bands GSM-900 --lte-bands LTE-800 LTE-1800
  python3 scan.py --stats
        """
    )
    parser.add_argument("--scan",        action="store_true")
    parser.add_argument("--import-ocid", action="store_true")
    parser.add_argument("--diagnose",    action="store_true")
    parser.add_argument("--stats",       action="store_true")
    parser.add_argument("--mode",        choices=["all","gsm","lte"], default="all")
    parser.add_argument("--gsm-bands",   nargs="+", choices=list(GSM_BANDS.keys()))
    parser.add_argument("--lte-bands",   nargs="+", choices=list(LTE_BANDS.keys()))
    parser.add_argument("--lat",         type=float)
    parser.add_argument("--lon",         type=float)
    parser.add_argument("--gain",        type=int,   default=None)
    parser.add_argument("--ppm",         type=int,   default=None)
    parser.add_argument("--duration",    type=int,   default=None)
    parser.add_argument("--gsm-threshold", type=float, default=None)
    parser.add_argument("--lte-threshold", type=float, default=None)
    parser.add_argument("--radius",      type=float, default=15)
    parser.add_argument("--token",       help="OpenCelliD API token")
    args = parser.parse_args()

    init_db()
    cfg = load_config()

    if args.lat:              cfg["lat"] = args.lat
    if args.lon:              cfg["lon"] = args.lon
    if args.gain is not None: cfg["gain"] = args.gain
    if args.ppm  is not None: cfg["ppm"]  = args.ppm
    if args.duration:         cfg["scan_duration"] = args.duration
    if args.gsm_threshold:    cfg["gsm_threshold"] = args.gsm_threshold
    if args.lte_threshold:    cfg["lte_threshold"] = args.lte_threshold
    if args.token:            cfg["opencellid_token"] = args.token
    save_config(cfg)

    if args.diagnose:
        diagnose()
    elif args.stats:
        show_stats()
    elif args.import_ocid:
        import_ocid(cfg, radius_km=args.radius)
    elif args.scan:
        run_scan(cfg, mode=args.mode,
                 gsm_bands=args.gsm_bands,
                 lte_bands=args.lte_bands)
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
