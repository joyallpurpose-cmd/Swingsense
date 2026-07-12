#!/usr/bin/env python3
"""
build_static.py - build the GitHub Pages version of SwingSense into docs/.

Takes dist/index.html (the live app) and produces docs/index.html: a
read-only copy that loads ./data.json directly instead of the /api/*
endpoints, with server-only controls (Run scan, Settings, Close buttons,
logs) hidden. Also copies data.json and the PWA assets.

Run after every scan you want published:  py build_static.py
(publish.bat does this plus the git push)
"""
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"
DOCS.mkdir(exist_ok=True)

html = (ROOT / "dist" / "index.html").read_text(encoding="utf-8")

# 1) static-mode bootstrap: replace the API loader with a data.json loader
static_loader = """
window.STATIC_MODE = true;
async function loadAll(){
  try {
    const db = await fetch('./data.json?t=' + Date.now()).then(r=>r.json());
    RECS = db.recommendations || [];
    CONFIG = db.config || {};
    renderTape(db.lastScan || null);
    renderSignals(); renderPositions(); renderPerformance();
  } catch(e){
    $('signalsWrap').innerHTML =
      '<div class="banner">Could not load data.json - has a scan been published yet?</div>';
  }
}
"""
# swap out the whole original loadAll (from its declaration to its closing brace)
m = re.search(r'async function loadAll\(\)\{[\s\S]*?\n\}', html)
assert m, "loadAll not found"
html = html[:m.start()] + static_loader.strip() + html[m.end():]

# 2) hide server-only UI: scan button, settings tab, per-row Close buttons,
#    close dialog, logs panel
html = html.replace('</style>', """
  /* --- static (GitHub Pages) mode --- */
  #scanBtn, #logs, #closeBox,
  nav button[data-tab="settings"],
  #positionsWrap button { display:none !important; }
  .staticnote{font-size:11.5px;color:var(--slate);text-align:center;margin-top:18px;}
</style>""")
html = html.replace('</main>',
    '<div class="staticnote">Read-only view published from the scanner. '
    'Run scans and close positions from the PC app.</div>\n</main>')

# 3) neutralize functions that touch the API in static mode
html = html.replace('async function runScan(){',
                    'async function runScan(){ if(window.STATIC_MODE) return;')
html = html.replace('async function saveConfig(ev){',
                    'async function saveConfig(ev){ if(window.STATIC_MODE){ev.preventDefault();return;}')
html = html.replace('async function confirmClose(){',
                    'async function confirmClose(){ if(window.STATIC_MODE) return;')
html = html.replace('function fillSettings(){',
                    'function fillSettings(){ if(window.STATIC_MODE) return;')
html = html.replace('function watchScan(oneShot){',
                    'function watchScan(oneShot){ if(window.STATIC_MODE) return;')

# 4) PWA paths must be relative on Pages (site lives under /<repo>/)
html = html.replace('href="/manifest.json"', 'href="./manifest.json"')
html = html.replace('href="/icon-192.png"', 'href="./icon-192.png"')
html = html.replace("navigator.serviceWorker.register('/sw.js')",
                    "navigator.serviceWorker.register('./sw.js')")

(DOCS / "index.html").write_text(html, encoding="utf-8")

# 5) copy data + PWA assets
if (ROOT / "data.json").exists():
    shutil.copy(ROOT / "data.json", DOCS / "data.json")
for f in ["manifest.json", "icon-192.png", "icon-512.png", "sw.js"]:
    src = ROOT / "dist" / f
    if src.exists():
        shutil.copy(src, DOCS / f)

# make the Pages manifest relative too
mf = DOCS / "manifest.json"
if mf.exists():
    m2 = json.loads(mf.read_text(encoding="utf-8"))
    m2["start_url"] = "./"
    m2["scope"] = "./"
    for ic in m2.get("icons", []):
        ic["src"] = ic["src"].lstrip("/")
    mf.write_text(json.dumps(m2, indent=1), encoding="utf-8")

print("docs/ built:", ", ".join(p.name for p in sorted(DOCS.iterdir())))
