# TowerScan Mk2

Passive **GSM (2G) + LTE (4G)** cell tower scanner and mapper.  
**Raspberry Pi 5** + **Nooelec NESDR SMArt XTR RTL-SDR**

## What's new in Mk2

| Feature | Mk1 | Mk2 |
|---------|-----|-----|
| GSM detection | ✓ rtl_power | ✓ rtl_power |
| GSM decode | ✗ broken gr-gsm | ✓ gr-gsm built from source |
| LTE detection | ✗ | ✓ rtl_power |
| LTE decode (PCI/EARFCN) | ✗ | ✓ LTE-Cell-Scanner |
| Map marker types | circle only | circle=GSM, diamond=LTE, triangle=raw |
| Radio filter | ✗ | ✓ GSM / LTE toggle |
| Schema migrations | ✗ crashes | ✓ automatic |
| Map centering | hardcoded | observer coordinates from DB |
| gr-gsm device.py fix | manual | applied during build |
| setup.sh rtl_test hang | ✗ | ✓ timeout wrapped |

## Setup

```bash
chmod +x setup.sh
sudo bash setup.sh
sudo reboot
```

Build time on Pi 5: ~10 min (gr-gsm) + ~5 min (LTE-Cell-Scanner)

## Download OpenCelliD data

Register free at **https://opencellid.org/register**, then:

```bash
# Czech Republic only (~10MB compressed)
wget -O cell_towers.csv.gz \
  "https://opencellid.org/ocid/downloads?token=YOUR_TOKEN&type=mcc&file=mcc-230.csv.gz"
```

Place the file in the same directory as `scan.py`.

## Quick start

```bash
# 1. Verify hardware and tools
python3 scan.py --diagnose

# 2. Pre-populate map from OCID database (instant)
python3 scan.py --import-ocid --lat 50.0759 --lon 14.4378

# 3. Run full scan (GSM + LTE)
python3 scan.py --scan --lat 50.0759 --lon 14.4378

# 4. GSM only
python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode gsm

# 5. LTE only
python3 scan.py --scan --lat 50.0759 --lon 14.4378 --mode lte

# 6. Open map
python3 server.py
# → http://localhost:5000  or  http://<pi-ip>:5000
```

## scan.py reference

```
--scan              Run RF scan
--import-ocid       Import towers from OCID CSV near your location
--diagnose          Hardware + software diagnostic
--stats             Database summary
--mode              all | gsm | lte  (default: all)
--gsm-bands         GSM-900 GSM-1800  (default: both)
--lte-bands         LTE-800 LTE-1800 LTE-2100 LTE-2600
--lat / --lon       Observer coordinates (saved to config.json)
--gain              SDR gain 0-50 (default 40)
--ppm               Frequency correction (default 0, XTR TCXO is accurate)
--duration          Seconds per band (default 60)
--gsm-threshold     dB above noise for GSM (default 12)
--lte-threshold     dB above noise for LTE (default 8, LTE is wider/flatter)
--radius            km radius for OCID import (default 15)
--token             OpenCelliD API token
```

## LTE bands (Czech Republic)

| Band | Downlink | Operators |
|------|----------|-----------|
| LTE-800 (B20) | 791–821 MHz | T-Mobile, O2, Vodafone |
| LTE-1800 (B3) | 1805–1880 MHz | T-Mobile, O2, Vodafone |
| LTE-2100 (B1) | 2110–2170 MHz | T-Mobile, O2, Vodafone |
| LTE-2600 (B7) | 2620–2690 MHz | T-Mobile, O2 |

## Map legend

| Marker | Meaning |
|--------|---------|
| Cyan circle | GSM-900 tower |
| Orange circle | GSM-1800 tower |
| Purple diamond | LTE-800 tower |
| Pink diamond | LTE-1800 tower |
| Teal diamond | LTE-2100 tower |
| Green diamond | LTE-2600 tower |
| Yellow triangle | Raw detection (carrier found, not yet decoded) |
| Faded marker | Tower in OCID database, no live signal detected |

## Tool stack

```
rtl_power          → sweeps bands, finds RF carriers (always available)
gr-gsm             → decodes GSM BCCH → MCC/MNC/LAC/CI
LTE-Cell-Scanner   → decodes LTE MIB  → EARFCN/PCI
OpenCelliD CSV     → offline tower database (lat/lon/operator)
OpenCelliD API     → online individual tower lookup
Mozilla Location   → online fallback
```

## Troubleshooting

**LTE scan finds nothing:**
- LTE signals look like flat noise to rtl_power — lower `--lte-threshold` to 5
- Make sure antenna is vertical
- LTE-2600 needs line of sight, try LTE-800 first (best range)

**GSM scan finds carriers but no tower identity:**
- gr-gsm may not be installed — run `--diagnose` to check
- Try `grgsm_scanner --band=GSM900 --gain=40` manually

**Map shows wrong location:**
- OCID proximity matching lists nearby towers, not exact matches
- Only gr-gsm decode gives exact Cell ID matching
- Use `--import-ocid` to pre-populate, then scan to confirm active towers

## Legal

Receives only unencrypted public broadcast signals (GSM BCCH, LTE MIB/SIB1).
No voice, SMS, or user data is intercepted or decoded.
