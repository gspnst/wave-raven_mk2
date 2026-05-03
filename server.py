#!/usr/bin/env python3
"""
TowerScan Mk2 — server.py
Serves the interactive tower map on http://0.0.0.0:5000
"""

import sqlite3
import json
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

DB_PATH = Path(__file__).parent / "towers.db"
PORT    = 5000

# ── Data access ───────────────────────────────────────────────────────────────

def q(sql, params=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows

def scalar(sql, params=(), default=0):
    conn = sqlite3.connect(DB_PATH)
    try:    val = conn.execute(sql, params).fetchone()[0]
    except: val = default
    conn.close()
    return val or default

def get_towers(operator=None, band=None, radio=None, mapped_only=False):
    where, p = [], []
    if operator:    where.append("operator LIKE ?");              p.append(f"%{operator}%")
    if band:        where.append("band = ?");                     p.append(band)
    if radio:       where.append("radio = ?");                    p.append(radio)
    if mapped_only: where.append("lat IS NOT NULL AND lon IS NOT NULL")
    sql = "SELECT * FROM towers"
    if where: sql += " WHERE " + " AND ".join(where)
    return q(sql + " ORDER BY last_seen DESC", p)

def get_detections(band=None, radio=None):
    where, p = [], []
    if band:  where.append("band = ?");  p.append(band)
    if radio: where.append("radio = ?"); p.append(radio)
    sql = "SELECT * FROM detections"
    if where: sql += " WHERE " + " AND ".join(where)
    return q(sql + " ORDER BY signal_dbm DESC", p)

def get_stats():
    s = {
        "total":       scalar("SELECT COUNT(*) FROM towers"),
        "mapped":      scalar("SELECT COUNT(*) FROM towers WHERE lat IS NOT NULL"),
        "detections":  scalar("SELECT COUNT(*) FROM detections"),
        "observations":scalar("SELECT COUNT(*) FROM observations"),
        "operators":   scalar("SELECT COUNT(DISTINCT operator) FROM towers"),
        "last_scan":   scalar("SELECT MAX(last_seen) FROM towers", default="never"),
    }
    try:
        for row in q("SELECT radio, COUNT(*) n FROM towers GROUP BY radio"):
            s[f"total_{row['radio'].lower()}"] = row["n"]
    except Exception:
        pass
    return s

def get_operators(): return [r["operator"] for r in q("SELECT DISTINCT operator FROM towers ORDER BY operator")]
def get_bands():     return [r["band"]     for r in q("SELECT DISTINCT band FROM towers WHERE band IS NOT NULL AND band!='' ORDER BY band")]
def get_radios():    return [r["radio"]    for r in q("SELECT DISTINCT radio FROM towers WHERE radio IS NOT NULL ORDER BY radio")]

# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>TowerScan Mk2</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600&family=Space+Mono:wght@400;700&display=swap">
<style>
:root{
  --bg:#0d0f14;--surface:#161a22;--surface2:#1e2430;
  --border:rgba(255,255,255,0.07);
  --cyan:#00e5ff;--orange:#ff6b35;--green:#00ff88;
  --yellow:#ffd700;--red:#ff4757;--purple:#b794f4;
  --text:#e2e8f0;--muted:#718096;
  --font:'Space Grotesk',sans-serif;--mono:'Space Mono',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--font);
     height:100vh;display:flex;flex-direction:column;overflow:hidden}

/* Header */
header{background:var(--surface);border-bottom:1px solid var(--border);
       padding:0 20px;height:52px;display:flex;align-items:center;gap:14px;
       flex-shrink:0;z-index:1000}
.logo{font-size:15px;font-weight:600;letter-spacing:.08em;
      text-transform:uppercase;color:var(--cyan);white-space:nowrap}
.logo span{color:var(--text)}
.logo small{font-size:10px;color:var(--muted);letter-spacing:.05em;margin-left:4px}
.hstats{display:flex;gap:18px;margin-left:auto}
.stat{display:flex;flex-direction:column;align-items:flex-end}
.sv{color:var(--cyan);font-weight:700;font-size:14px;font-family:var(--mono)}
.sl{color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em}

/* Layout */
main{display:flex;flex:1;overflow:hidden}

/* Sidebar */
#sidebar{width:300px;background:var(--surface);border-right:1px solid var(--border);
         display:flex;flex-direction:column;overflow:hidden;flex-shrink:0}
.fp{padding:14px 16px;border-bottom:1px solid var(--border);
    display:flex;flex-direction:column;gap:10px}
.fp label{font-size:11px;color:var(--muted);text-transform:uppercase;
          letter-spacing:.05em;margin-bottom:3px;display:block}
select{width:100%;background:var(--surface2);border:1px solid var(--border);
       color:var(--text);padding:7px 10px;border-radius:6px;
       font-family:var(--font);font-size:13px;cursor:pointer}
.tog{display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer}
input[type=checkbox]{accent-color:var(--cyan)}
.frow{display:flex;gap:8px}
.frow>div{flex:1}

#list{flex:1;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--surface2) transparent}
.ti{padding:12px 16px;border-bottom:1px solid var(--border);cursor:pointer;transition:background .15s}
.ti:hover{background:var(--surface2)}
.ti.active{background:rgba(0,229,255,.07);border-left:2px solid var(--cyan)}
.tn{font-size:13px;font-weight:500}
.tm{font-size:11px;color:var(--muted);font-family:var(--mono);margin-top:3px}
.badges{display:flex;gap:4px;margin-top:5px;flex-wrap:wrap}
.badge{font-size:10px;padding:2px 6px;border-radius:3px;font-family:var(--mono);font-weight:700}
.b-gsm  {background:rgba(0,229,255,.12);  color:var(--cyan)}
.b-lte  {background:rgba(183,148,244,.12);color:var(--purple)}
.b-raw  {background:rgba(255,215,0,.12);  color:var(--yellow)}
.b-nogps{background:rgba(255,107,53,.12); color:var(--orange)}
.b-sig  {background:rgba(0,255,136,.12);  color:var(--green)}
.b-sig.mid {background:rgba(255,215,0,.12);color:var(--yellow)}
.b-sig.weak{background:rgba(255,71,87,.12);color:var(--red)}
.b-band {background:rgba(255,255,255,.06);color:var(--muted)}

/* Map */
#mw{position:relative;flex:1;min-height:0}
#map{width:100%;height:100%}

/* Info panel */
#info{position:absolute;bottom:20px;right:20px;width:290px;
      background:var(--surface);border:1px solid var(--border);
      border-radius:10px;padding:16px;z-index:900;display:none;
      box-shadow:0 8px 32px rgba(0,0,0,.5)}
#info h3{font-size:14px;color:var(--cyan);margin-bottom:10px}
#info table{width:100%;border-collapse:collapse;font-size:12px}
#info td{padding:3px 0;vertical-align:top}
#info td:first-child{color:var(--muted);width:90px;font-size:11px;
                     text-transform:uppercase;letter-spacing:.04em}
#info td:last-child{color:var(--text);font-family:var(--mono);word-break:break-all}
#ix{position:absolute;top:10px;right:12px;cursor:pointer;
    color:var(--muted);font-size:18px;line-height:1}
#ix:hover{color:var(--text)}

.pulse{display:inline-block;width:7px;height:7px;border-radius:50%;
       background:var(--green);margin-right:6px;animation:pulse 2s infinite}
@keyframes pulse{
  0%,100%{opacity:1;box-shadow:0 0 0 0 rgba(0,255,136,.4)}
  50%{opacity:.6;box-shadow:0 0 0 5px rgba(0,255,136,0)}
}
.empty{padding:32px 16px;text-align:center;color:var(--muted);font-size:13px}
.empty code{display:block;margin-top:10px;font-family:var(--mono);font-size:11px;
            color:var(--cyan);background:var(--surface2);padding:10px;
            border-radius:4px;text-align:left;line-height:1.6}

/* Leaflet overrides */
.leaflet-popup-content-wrapper{background:var(--surface)!important;
  color:var(--text)!important;border:1px solid var(--border)!important;
  border-radius:8px!important;box-shadow:0 4px 20px rgba(0,0,0,.5)!important}
.leaflet-popup-tip{background:var(--surface)!important}
.leaflet-popup-content{font-family:var(--font)}
</style>
</head>
<body>
<header>
  <span class="pulse"></span>
  <div class="logo">TOWER<span>SCAN</span><small>MK2</small></div>
  <div class="hstats">
    <div class="stat"><span class="sv" id="h-gsm">–</span><span class="sl">GSM</span></div>
    <div class="stat"><span class="sv" id="h-lte">–</span><span class="sl">LTE</span></div>
    <div class="stat"><span class="sv" id="h-det">–</span><span class="sl">Detections</span></div>
    <div class="stat"><span class="sv" id="h-mapped">–</span><span class="sl">Mapped</span></div>
    <div class="stat"><span class="sv" id="h-ops">–</span><span class="sl">Operators</span></div>
  </div>
</header>
<main>
  <div id="sidebar">
    <div class="fp">
      <div class="frow">
        <div><label>Radio</label>
          <select id="f-radio">
            <option value="">All</option>
            <option value="GSM">GSM</option>
            <option value="LTE">LTE</option>
          </select>
        </div>
        <div><label>Band</label>
          <select id="f-band"><option value="">All bands</option></select>
        </div>
      </div>
      <div><label>Operator</label>
        <select id="f-op"><option value="">All operators</option></select>
      </div>
      <label class="tog"><input type="checkbox" id="f-mapped"> GPS coordinates only</label>
      <label class="tog"><input type="checkbox" id="f-det" checked> Show raw detections</label>
    </div>
    <div id="list"></div>
  </div>
  <div id="mw">
    <div id="map"></div>
    <div id="info">
      <span id="ix">×</span>
      <h3 id="i-title">Details</h3>
      <table id="i-table"></table>
    </div>
  </div>
</main>

<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
let items=[], mkrs={}, map, layer;

const COLORS = {
  'GSM-900':'#00e5ff','GSM-1800':'#ff6b35',
  'LTE-800':'#b794f4','LTE-1800':'#f687b3',
  'LTE-2100':'#76e4f7','LTE-2600':'#68d391',
};
const RADIO_COLOR = { GSM:'#00e5ff', LTE:'#b794f4' };

// Map
const dark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',
  {attribution:'© OSM © CARTO',maxZoom:19,subdomains:'abcd'});
const osm  = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
  {attribution:'© OpenStreetMap',maxZoom:19});
map = L.map('map',{center:[50.07,14.43],zoom:13,layers:[dark],zoomControl:false});
L.control.zoom({position:'bottomright'}).addTo(map);
L.control.layers({'Dark':dark,'Street':osm}).addTo(map);
layer = L.layerGroup().addTo(map);

function icon(item) {
  const color = item._raw
    ? '#ffd700'
    : (COLORS[item.band] || RADIO_COLOR[item.radio] || '#a0aec0');
  const isLTE = item.radio === 'LTE';
  const hasGps = !!(item.lat && item.lon);
  const op = !item._raw ? 0.9 : 0.65;

  // LTE = diamond, GSM = circle, raw = triangle
  let shape;
  if (item._raw) {
    shape = `<polygon points="14,3 25,23 3,23" fill="${color}" fill-opacity="0.25"
               stroke="${color}" stroke-width="1.5"/>`;
  } else if (isLTE) {
    shape = `<polygon points="14,2 26,14 14,26 2,14" fill="${color}" fill-opacity="0.2"
               stroke="${color}" stroke-width="1.5"/>
             <circle cx="14" cy="14" r="4" fill="${color}"/>`;
  } else {
    shape = `<circle cx="14" cy="14" r="11" fill="${color}" fill-opacity="0.15"
               stroke="${color}" stroke-width="1.5"/>
             <circle cx="14" cy="14" r="4" fill="${color}"/>`;
  }
  const ring = hasGps && !item._raw
    ? `<circle cx="14" cy="14" r="8" fill="none" stroke="${color}" stroke-width="0.8" opacity="0.4"/>`
    : '';
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="28" height="36"
    viewBox="0 0 28 36" style="opacity:${op}">
    ${shape}${ring}
    <line x1="14" y1="${item._raw?23:25}" x2="14" y2="34"
          stroke="${color}" stroke-width="1.5"/>
  </svg>`;
  return L.divIcon({html:svg,className:'',iconSize:[28,36],
                    iconAnchor:[14,34],popupAnchor:[0,-32]});
}

function sigBadge(dbm) {
  if (!dbm) return '';
  const c = dbm > -70 ? 'b-sig' : dbm > -85 ? 'b-sig mid' : 'b-sig weak';
  return `<span class="badge ${c}">${dbm} dBm</span>`;
}

function radioBadge(item) {
  if (item._raw) return `<span class="badge b-raw">unidentified</span>`;
  if (item.radio === 'LTE') return `<span class="badge b-lte">LTE</span>`;
  return `<span class="badge b-gsm">GSM</span>`;
}

function popup(item) {
  const title = item._raw
    ? (item.radio==='LTE'?`EARFCN ${item.arfcn||'?'}`:`ARFCN ${item.arfcn||'?'}`) + ' (unidentified)'
    : (item.operator || 'Unknown');
  const id = item._raw
    ? `${item.freq_mhz} MHz &bull; ${item.band}`
    : item.radio==='LTE'
      ? `MCC ${item.mcc} / MNC ${item.mnc}<br>TAC ${item.lac} / CI ${item.cell_id}${item.pci?`<br>PCI ${item.pci}`:''}`
      : `MCC ${item.mcc} / MNC ${item.mnc}<br>LAC ${item.lac} / CI ${item.cell_id}`;
  const gps = item.lat
    ? `<span style="color:var(--green)">📍 ${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}</span>`
    : `<span style="color:var(--orange)">⚠ No GPS</span>`;
  return `<div style="font-family:var(--font);min-width:190px">
    <div style="font-size:14px;font-weight:600;color:var(--cyan);margin-bottom:8px">${title}</div>
    <div style="font-size:11px;color:var(--muted);font-family:var(--mono);line-height:1.7">
      ${id}<br>${gps}
      ${item.signal_dbm?`<br>${item.signal_dbm} dBm`:''}
      ${item._raw?`<br><em style="color:var(--yellow)">Carrier detected — no identity decoded yet</em>`:''}
    </div></div>`;
}

function showInfo(item) {
  document.getElementById('i-title').textContent =
    item._raw ? `${item.radio} ${item.arfcn?'ARFCN/EARFCN '+item.arfcn:'detection'}` : (item.operator||'Unknown');
  const rows = item._raw
    ? [['Type',`Raw ${item.radio} detection`],['Band',item.band],
       ['Frequency',`${item.freq_mhz} MHz`],
       [item.radio==='LTE'?'EARFCN':'ARFCN', item.arfcn||'N/A'],
       ['PCI', item.pci||'N/A'],
       ['Signal',`${item.signal_dbm} dBm`],
       ['Noise',`${item.noise_floor} dBm`],
       ['Detected',item.timestamp?.slice(0,16)||'N/A']]
    : [['Radio',item.radio],['Operator',item.operator],['Country',item.country],
       ['MCC/MNC',`${item.mcc}/${item.mnc}`],
       [item.radio==='LTE'?'TAC':'LAC', item.lac],
       ['Cell ID',item.cell_id],
       ...(item.pci?[['PCI',item.pci]]:[]),
       ['Band',item.band||'N/A'],['Frequency',item.freq_mhz?`${item.freq_mhz} MHz`:'N/A'],
       ['Signal',item.signal_dbm?`${item.signal_dbm} dBm`:'N/A'],
       ['Range',item.range_m?`~${item.range_m}m`:'N/A'],
       ['GPS',item.lat?`${item.lat.toFixed(5)}, ${item.lon.toFixed(5)}`:'not found'],
       ['Seen',item.seen_count||1],
       ['Last seen',item.last_seen?.slice(0,16)||'N/A']];
  document.getElementById('i-table').innerHTML =
    rows.map(([k,v])=>`<tr><td>${k}</td><td>${v??'N/A'}</td></tr>`).join('');
  document.getElementById('info').style.display = 'block';
}

function renderList(filtered) {
  const el = document.getElementById('list');
  if (!filtered.length) {
    el.innerHTML = `<div class="empty">No data yet.
      <code>python3 scan.py --diagnose
python3 scan.py --import-ocid --lat LAT --lon LON
python3 scan.py --scan --lat LAT --lon LON</code></div>`;
    return;
  }
  el.innerHTML = filtered.map((item,i) => {
    const name = item._raw
      ? `${item.radio} ${item.radio==='LTE'?'EARFCN':'ARFCN'} ${item.arfcn||'?'} — ${item.band||''}`
      : (item.operator||'Unknown');
    const meta = item._raw
      ? `${item.freq_mhz} MHz &bull; ${item.signal_dbm} dBm`
      : item.radio==='LTE'
        ? `TAC ${item.lac} / CI ${item.cell_id}${item.pci?' / PCI '+item.pci:''} &bull; ${item.freq_mhz||'?'} MHz`
        : `LAC ${item.lac} / CI ${item.cell_id} &bull; ${item.freq_mhz||'?'} MHz`;
    return `<div class="ti" data-i="${i}" onclick="sel(${i})">
      <div class="tn">${name}</div>
      <div class="tm">${meta}</div>
      <div class="badges">
        ${radioBadge(item)}
        ${item.band?`<span class="badge b-band">${item.band}</span>`:''}
        ${sigBadge(item.signal_dbm)}
        ${!item.lat&&!item._raw?`<span class="badge b-nogps">no GPS</span>`:''}
      </div></div>`;
  }).join('');
}

function renderMarkers(filtered) {
  layer.clearLayers(); mkrs = {};
  filtered.forEach((item,i) => {
    if (!item.lat || !item.lon) return;
    const m = L.marker([item.lat,item.lon],{icon:icon(item)})
      .bindPopup(popup(item)).addTo(layer);
    m.on('click',()=>sel(i));
    mkrs[i] = m;
  });
}

function sel(i) {
  document.querySelectorAll('.ti').forEach(e=>e.classList.remove('active'));
  const el = document.querySelector(`.ti[data-i="${i}"]`);
  if (el){ el.classList.add('active'); el.scrollIntoView({block:'nearest'}); }
  const item = items[i];
  showInfo(item);
  if (item.lat && item.lon){
    map.setView([item.lat,item.lon],15);
    mkrs[i]?.openPopup();
  }
}

function filter() {
  const radio  = document.getElementById('f-radio').value;
  const band   = document.getElementById('f-band').value;
  const op     = document.getElementById('f-op').value;
  const mapped = document.getElementById('f-mapped').checked;
  const dets   = document.getElementById('f-det').checked;

  const f = items.filter(item => {
    if (!dets && item._raw)               return false;
    if (radio && item.radio !== radio)    return false;
    if (band  && item.band  !== band)     return false;
    if (op    && item.operator !== op)    return false;
    if (mapped && (!item.lat||!item.lon)) return false;
    return true;
  });
  renderList(f);
  renderMarkers(f);
}

['f-radio','f-band','f-op','f-mapped','f-det'].forEach(id =>
  document.getElementById(id).addEventListener('change', filter));
document.getElementById('ix').addEventListener('click',()=>
  document.getElementById('info').style.display='none');

async function load() {
  try {
    const [towers, stats, ops, bands, dets] = await Promise.all([
      fetch('/api/towers').then(r=>r.json()),
      fetch('/api/stats').then(r=>r.json()),
      fetch('/api/operators').then(r=>r.json()),
      fetch('/api/bands').then(r=>r.json()),
      fetch('/api/detections').then(r=>r.json()),
    ]);

    const knownArfcns = new Set(towers.map(t=>t.arfcn).filter(Boolean));
    const rawDets = dets
      .filter(d => !knownArfcns.has(d.arfcn))
      .map(d => ({...d, _raw:true, lat:d.observer_lat, lon:d.observer_lon}));

    items = [...towers, ...rawDets];

    // Header stats
    document.getElementById('h-gsm').textContent   = stats.total_gsm  || 0;
    document.getElementById('h-lte').textContent   = stats.total_lte  || 0;
    document.getElementById('h-det').textContent   = stats.detections || 0;
    document.getElementById('h-mapped').textContent = stats.mapped    || 0;
    document.getElementById('h-ops').textContent   = stats.operators  || 0;

    // Dropdowns
    document.getElementById('f-op').innerHTML =
      '<option value="">All operators</option>' +
      ops.map(o=>`<option value="${o}">${o}</option>`).join('');
    document.getElementById('f-band').innerHTML =
      '<option value="">All bands</option>' +
      bands.map(b=>`<option value="${b}">${b}</option>`).join('');

    filter();

    // Centre on observer location
    const obs = towers[0] || dets[0];
    if (obs?.observer_lat && obs?.observer_lon) {
      const pts = items.filter(i=>i.lat&&i.lon);
      if (pts.length > 1) {
        const lats = pts.map(i=>i.lat), lons = pts.map(i=>i.lon);
        map.fitBounds([
          [Math.min(...lats)-0.003, Math.min(...lons)-0.003],
          [Math.max(...lats)+0.003, Math.max(...lons)+0.003],
        ]);
      } else {
        map.setView([obs.observer_lat, obs.observer_lon], 15);
      }
    }
  } catch(e) { console.error(e); }
}

load();
setInterval(load, 30000);
</script>
</body>
</html>"""

# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): pass

    def send_json(self, data):
        b = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(b))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(b)

    def send_html(self, html):
        b = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(b))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        p  = urlparse(self.path)
        qs = parse_qs(p.query)
        G  = lambda k: qs.get(k, [None])[0]

        routes = {
            "/":               lambda: self.send_html(HTML),
            "/index.html":     lambda: self.send_html(HTML),
            "/api/stats":      lambda: self.send_json(get_stats()),
            "/api/operators":  lambda: self.send_json(get_operators()),
            "/api/bands":      lambda: self.send_json(get_bands()),
            "/api/radios":     lambda: self.send_json(get_radios()),
            "/api/towers":     lambda: self.send_json(
                get_towers(G("operator"), G("band"), G("radio"),
                           G("mapped_only") == "1")),
            "/api/detections": lambda: self.send_json(
                get_detections(G("band"), G("radio"))),
        }
        h = routes.get(p.path)
        if h:   h()
        else:   self.send_response(404); self.end_headers()

if __name__ == "__main__":
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH} — run scan.py first.")
    else:
        s = get_stats()
        print(f"TowerScan Mk2")
        print(f"  GSM: {s.get('total_gsm',0)} | LTE: {s.get('total_lte',0)} | "
              f"Detections: {s['detections']} | Mapped: {s['mapped']}")
    print(f"\nOpen → http://localhost:{PORT}")
    print("Ctrl+C to stop.\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:    server.serve_forever()
    except KeyboardInterrupt: print("\nStopped.")
