#!/usr/bin/env python3
"""
SKYDLE - a daily aircraft guessing game (website edition), built from your Skycards CSV.
No extra packages needed, just Python 3.8+.

  python skydle.py                 shows a menu (you can also just double-click skydle.py): daily game, test mode, or self-test
  python skydle.py --daily         skip the menu and start the daily game
  python skydle.py --testmode      skip the menu and start test mode (pick any answer, reveal it, reset, preview dates)
  python skydle.py --selftest      run automated checks and print PASS/FAIL for each
  python skydle.py --csv "path"    use a CSV somewhere else
  python skydle.py --port 9000     use a different port

Photos: put images in a folder called  photos  next to this script, named by ICAO
code (A388.jpg, B738.png, ...). If a plane has no photo, the page says "No picture available".

Photo credits: list them in  photos/credits.csv  (columns: icao, photographer, license, source_url).
The credit appears under the photo. The file is created for you the first time you run the game.
"""

import argparse
import csv
import json
import mimetypes
import random
import re
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MAX_GUESSES = 7
MIN_FILLED_STATS = 4  # a plane needs at least this many of the 6 hints to be an answer

# Where to look for the CSV (first one that exists wins). You can also use --csv.
CSV_CANDIDATES = [
    Path(r"C:\Codes\Skycards stuff\Daily game\Skycards Weekly Battle Options - Anything Goes.csv"),
    HERE / "Skycards Weekly Battle Options - Anything Goes.csv",
    HERE / "Skycards_Weekly_Battle_Options_-_Anything_Goes.csv",
]
PHOTO_DIR = HERE / "photos"
PHOTO_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

# Force a specific Wikipedia page for a plane if the automatic lookup is wrong:
# {"A388": "Airbus A380"}
WIKI_OVERRIDES = {}

# (label, csv column index, unit)
STATS = [
    ("Year", 6, ""),
    ("Wingspan", 7, " m"),
    ("Speed", 8, " kts"),
    ("Weight", 9, " t"),
    ("Seats", 10, ""),
    ("Rarity", 11, ""),
]


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
# Brand = the manufacturer at the start of the aircraft name (BOEING, AIRBUS, ANTONOV...).
# The names in the CSV start with the brand in CAPITALS, but a few have all-caps model codes
# (WACO D, HINDUSTAN LCA...) so these corrections trim them back to the real brand.
BRAND_FIXES = {
    "BRITISH AEROSPACE ATP": "BRITISH AEROSPACE", "CONSOLIDATED PBY": "CONSOLIDATED",
    "DOUGLAS SBD": "DOUGLAS", "GRUMMAN TBF": "GRUMMAN", "FOKKER F.VII": "FOKKER",
    "HINDUSTAN ALH": "HINDUSTAN", "HINDUSTAN LCA": "HINDUSTAN", "ALENIA AMX": "ALENIA",
    "FLIGHT DESIGN CT": "FLIGHT DESIGN", "FLIGHT DESIGN MC": "FLIGHT DESIGN",
    "MXR MXS": "MXR", "NEMESIS NXT": "NEMESIS", "EXTRA NG": "EXTRA", "REMOS GX": "REMOS",
    "ROBIN ATL": "ROBIN", "TAYLORCRAFT BC": "TAYLORCRAFT", "TAYLORCRAFT DC": "TAYLORCRAFT",
    "WACO AGC": "WACO", "WACO D": "WACO", "WACO E": "WACO", "WACO F": "WACO",
    "RUSCHMEYER R": "RUSCHMEYER", "SMYTH S": "SMYTH", "WASSMER WA": "WASSMER",
    "RAJ HAMSA X-AIR H": "RAJ HAMSA", "RAJ HAMSA X-AIR S": "RAJ HAMSA",
    "DTA J-RO": "DTA", "SONEX JSX": "SONEX",
    "UFO": "",  # not a real manufacturer
}
# Names that start with a digit or are not in capitals (matched by start of name, in capitals)
BRAND_PREFIXES = {
    "G1 AVIATION": "G1 AVIATION", "3XTRIM": "3XTRIM", "A2 CZ": "A2 CZ", "C2P": "C2P",
}
# Optional: treat two brands as the same, e.g. {"AIRBUS HELICOPTERS": "AIRBUS",
# "DE HAVILLAND CANADA": "DE HAVILLAND"}. Empty by default: brands must match exactly.
BRAND_ALIASES = {}


def extract_brand(name):
    """Return the manufacturer of an aircraft name, or None (Drone, Balloon, Glider...)."""
    n = name.replace("\\xa0", " ").strip()
    up = n.upper()
    for prefix, brand in BRAND_PREFIXES.items():
        if up.startswith(prefix):
            return BRAND_ALIASES.get(brand, brand)
    lead = []
    for w in n.split():
        if re.search(r"\d", w) or not (w == "&" or (w.upper() == w and re.search("[A-Z]", w))):
            break
        lead.append(w)
    brand = " ".join(lead)
    brand = BRAND_FIXES.get(brand, brand)
    brand = BRAND_ALIASES.get(brand, brand)
    return brand or None


def compare_brand(guess_brand, answer_brand):
    if not answer_brand:
        return "nodata"
    if not guess_brand:
        return "unknown"
    return "correct" if guess_brand.upper() == answer_brand.upper() else "wrong"


def resolve_csv(explicit=None):
    """Return (path or None, list of places we looked)."""
    tried = []
    candidates = [Path(explicit)] if explicit else list(CSV_CANDIDATES)
    if not explicit:
        # Any CSV next to the script with "skycards" in its name
        candidates += sorted(p for p in HERE.glob("*.csv") if "skycards" in p.name.lower())
    for p in candidates:
        tried.append(str(p))
        if p.is_file():
            return p, tried
    return None, tried


def to_num(s):
    try:
        return float((s or "").strip())
    except ValueError:
        return None


def load_planes(path):
    planes = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 12:
                continue
            icao, name = row[2].strip(), row[3].replace("\xa0", " ").strip()
            if not icao or not name or icao.upper() == "ICAO":
                continue
            plane = {"icao": icao, "name": name, "brand": extract_brand(name)}
            for label, idx, _ in STATS:
                plane[label] = to_num(row[idx])
            planes.append(plane)
    return planes


def filled_count(plane):
    return sum(plane[l] is not None for l, _, _ in STATS)


def eligible_planes(planes):
    return sorted(
        (p for p in planes if filled_count(p) >= MIN_FILLED_STATS),
        key=lambda p: p["icao"],
    )


def daily_answer(planes, day):
    return random.Random(day.isoformat()).choice(eligible_planes(planes))


def compare(guess_val, answer_val):
    """Where is the ANSWER relative to the guess?"""
    if answer_val is None:
        return "nodata"
    if guess_val is None:
        return "unknown"
    if guess_val == answer_val:
        return "correct"
    return "higher" if answer_val > guess_val else "lower"


def judge(guess, answer):
    return {
        "icao": guess["icao"],
        "name": guess["name"],
        "correct": guess["icao"] == answer["icao"],
        "brand": {"value": guess["brand"], "result": compare_brand(guess["brand"], answer["brand"])},
        "stats": [
            {"label": l, "unit": u, "value": guess[l], "result": compare(guess[l], answer[l])}
            for l, _, u in STATS
        ],
    }


# --------------------------------------------------------------------------- #
# Photos & Wikipedia
# --------------------------------------------------------------------------- #
def find_photo(photo_dir, icao):
    """Case-insensitive lookup of photos/<ICAO>.<ext>. Returns a Path or None."""
    try:
        for f in Path(photo_dir).iterdir():
            if f.is_file() and f.suffix.lower() in PHOTO_EXTS and f.stem.upper() == icao.upper():
                return f
    except OSError:
        pass
    return None


def _col(row, *names):
    for n in names:
        v = (row.get(n) or "").strip()
        if v:
            return v
    return ""


def get_credit(photo_dir, icao):
    """Look up the photo credit in <photo_dir>/credits.csv. Returns a dict or None."""
    f = Path(photo_dir) / "credits.csv"
    try:
        with open(f, encoding="utf-8-sig", newline="") as fh:
            for raw in csv.DictReader(fh):
                row = {(k or "").strip().lower(): v for k, v in raw.items()}
                if _col(row, "icao").upper() != icao.upper():
                    continue
                who = _col(row, "photographer", "credit", "author")
                if not who:
                    return None
                url = _col(row, "source_url", "url", "source", "link")
                if not re.match(r"^https?://", url, re.I):
                    url = ""  # only ever allow normal web links
                return {"photographer": who, "license": _col(row, "license", "licence"), "url": url}
    except OSError:
        pass
    return None


def ensure_photo_setup(photo_dir):
    """Create the photos folder and an empty credits.csv template if they don't exist."""
    try:
        photo_dir.mkdir(parents=True, exist_ok=True)
        c = photo_dir / "credits.csv"
        if not c.exists():
            c.write_text("icao,photographer,license,source_url\n", encoding="utf-8")
    except OSError:
        pass


_wiki_cache = {}


def wikipedia_url(plane):
    icao = plane["icao"]
    if icao in _wiki_cache:
        return _wiki_cache[icao]
    url = None
    title = WIKI_OVERRIDES.get(icao)
    if title:
        url = "https://en.wikipedia.org/wiki/" + urllib.parse.quote(title.replace(" ", "_"))
    else:
        try:
            q = urllib.parse.urlencode({"action": "opensearch", "search": plane["name"],
                                        "limit": 1, "namespace": 0, "format": "json"})
            req = urllib.request.Request(f"https://en.wikipedia.org/w/api.php?{q}",
                                         headers={"User-Agent": "SkydleGame/1.0"})
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.load(r)
            if data[3]:
                url = data[3][0]
        except Exception:
            pass
    if not url:  # offline or no hit: fall back to a search link
        url = "https://en.wikipedia.org/w/index.php?search=" + urllib.parse.quote(plane["name"])
    _wiki_cache[icao] = url
    return url


# --------------------------------------------------------------------------- #
# Game object + HTTP server
# --------------------------------------------------------------------------- #
class Game:
    def __init__(self, planes, photo_dir, test_mode=False):
        self.planes = planes
        self.by_icao = {p["icao"].upper(): p for p in planes}
        self.photo_dir = Path(photo_dir)
        self.test_mode = test_mode
        self.test_answer = None  # test mode: forced answer
        self.test_date = None    # test mode: previewed date

    def today(self):
        return self.test_date or date.today()

    def answer(self):
        return self.test_answer or daily_answer(self.planes, self.today())

    def state(self):
        ans = self.answer()
        photo = find_photo(self.photo_dir, ans["icao"])
        s = {
            "date": self.today().isoformat(),
            "max_guesses": MAX_GUESSES,
            "test_mode": self.test_mode,
            "has_photo": photo is not None,
            "credit": get_credit(self.photo_dir, ans["icao"]) if photo else None,
        }
        if self.test_mode:  # only ever reveal the answer in test mode
            s["answer"] = {"icao": ans["icao"], "name": ans["name"]}
        return s


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        game = self.server.game
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        path = u.path

        if path == "/":
            return self._send(PAGE, "text/html; charset=utf-8")
        if path == "/api/state":
            return self._send(game.state())
        if path == "/api/planes":
            return self._send([{"icao": p["icao"], "name": p["name"]} for p in game.planes])

        if path == "/api/guess":
            guess = game.by_icao.get(q.get("icao", "").upper())
            if not guess:
                return self._send({"error": "Unknown plane"}, code=400)
            return self._send(judge(guess, game.answer()))

        if path == "/api/reveal":
            ans = game.answer()
            return self._send({
                "icao": ans["icao"], "name": ans["name"], "brand": ans["brand"], "wiki": wikipedia_url(ans),
                "stats": [{"label": l, "unit": un, "value": ans[l]} for l, _, un in STATS],
            })

        if path == "/photo/today":
            photo = find_photo(game.photo_dir, game.answer()["icao"])
            if not photo:
                return self._send({"error": "no photo"}, code=404)
            ctype = mimetypes.guess_type(photo.name)[0] or "application/octet-stream"
            return self._send(photo.read_bytes(), ctype)

        # ---- test-mode endpoints ----
        if path.startswith("/api/test/") and game.test_mode:
            if path == "/api/test/planes":
                return self._send([{"icao": p["icao"], "name": p["name"], "filled": filled_count(p),
                                    "eligible": filled_count(p) >= MIN_FILLED_STATS}
                                   for p in game.planes])
            if path == "/api/test/set":
                v = q.get("icao", "")
                if v == "random":
                    game.test_answer = random.choice(eligible_planes(game.planes))
                elif v.upper() in game.by_icao:
                    game.test_answer = game.by_icao[v.upper()]
                else:
                    return self._send({"error": "Unknown plane"}, code=400)
                return self._send(game.state())
            if path == "/api/test/date":
                try:
                    game.test_date = datetime.strptime(q.get("d", ""), "%Y-%m-%d").date()
                    game.test_answer = None
                except ValueError:
                    return self._send({"error": "Use YYYY-MM-DD"}, code=400)
                return self._send(game.state())
            if path == "/api/test/clear":
                game.test_answer = None
                game.test_date = None
                return self._send(game.state())

        self._send({"error": "not found"}, code=404)


def make_server(game, port):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.game = game
    return srv


# --------------------------------------------------------------------------- #
# The web page
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Skydle - Daily Aircraft Guess</title>
<style>
:root{--bg1:#0b1a33;--bg2:#17335f;--card:#13264a;--line:#2a4577;--txt:#eaf1ff;--mut:#9db3d8;
--ok:#1f9d57;--hi:#c98a12;--lo:#3b7bd8;--nd:#555e70;}
*{box-sizing:border-box}
body{margin:0;font-family:system-ui,Segoe UI,Roboto,sans-serif;color:var(--txt);
background:linear-gradient(180deg,var(--bg1),var(--bg2));min-height:100vh}
.wrap{max-width:900px;margin:0 auto;padding:20px 14px 60px}
h1{margin:6px 0 2px;font-size:2rem;letter-spacing:1px}
.sub{color:var(--mut);margin:0 0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin-bottom:14px}
#photoBox{height:300px;display:flex;align-items:center;justify-content:center;overflow:hidden;
background:#0a1530;border-radius:10px}
#photoBox img{max-width:100%;max-height:100%;object-fit:contain}
.nopic{color:var(--mut);font-size:1.1rem;text-align:center}
.nopic span{display:block;font-size:2.4rem;margin-bottom:6px}
.row{display:flex;gap:8px;align-items:center;position:relative}
input,select,button{font:inherit;border-radius:10px;border:1px solid var(--line);background:#0d1d3d;color:var(--txt);padding:10px 12px}
input{flex:1;min-width:0}
button{cursor:pointer;background:#2a5bd7;border-color:#2a5bd7;font-weight:600}
button:disabled{opacity:.5;cursor:default}
button.alt{background:#0d1d3d;border-color:var(--line)}
#sugg{position:absolute;top:100%;left:0;right:90px;z-index:10;background:#0d1d3d;border:1px solid var(--line);
border-radius:10px;margin-top:4px;overflow:hidden}
#sugg div{padding:9px 12px;cursor:pointer;display:flex;justify-content:space-between;gap:10px}
#sugg div.on,#sugg div:hover{background:#1c3a72}
#sugg small{color:var(--mut)}
#msg{min-height:1.3em;color:#ffb4b4;margin:8px 2px 0}
.count{color:var(--mut);margin:0 0 8px}
.scroll{overflow-x:auto}
table{border-collapse:separate;border-spacing:4px;width:100%;min-width:760px}
th{font-size:.75rem;color:var(--mut);text-transform:uppercase;font-weight:600;padding:2px 4px;text-align:center}
th:first-child,td.nm{text-align:left}
td{border-radius:8px;padding:8px 6px;text-align:center;background:var(--nd);font-size:.95rem;min-width:84px}
td.nm{background:transparent;padding-left:2px;font-weight:600;min-width:170px}
td.nm small{display:block;color:var(--mut);font-weight:400}
td .a{font-size:.72rem;opacity:.92;margin-top:2px}
td.ok{background:var(--ok)} td.hi{background:var(--hi)} td.lo{background:var(--lo)} td.bad{background:#b03a3a}
#result h2{margin:0 0 6px}
#result a{color:#8fc1ff;font-weight:600;word-break:break-all}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chips span{background:#0d1d3d;border:1px solid var(--line);border-radius:999px;padding:3px 10px;font-size:.85rem}
.legend{color:var(--mut);font-size:.85rem;margin-top:6px}
.credit{font-size:.8rem;color:var(--mut);margin-top:8px;text-align:center;min-height:1em}
.credit a{color:#8fc1ff}
.credit .warn{color:#ffb4b4}
#test{border-color:#c0392b}
#test h3{margin:0 0 8px;color:#ff9c91}
#test .row{margin-bottom:8px;flex-wrap:wrap}
#test select{flex:1;min-width:220px}
#ans{font-weight:700;color:#ffd27a}
</style></head><body><div class="wrap">
<h1>&#9992;&#65039; SKYDLE</h1>
<p class="sub" id="date"></p>

<div class="card" id="test" hidden>
  <h3>TEST MODE</h3>
  <div>Current answer: <span id="ans"></span></div>
  <div class="row" style="margin-top:8px">
    <select id="tsel"></select>
    <button class="alt" id="tset">Use this answer</button>
    <button class="alt" id="trand">Random</button>
  </div>
  <div class="row">
    <input type="date" id="tdate" style="flex:0 0 auto">
    <button class="alt" id="tdset">Preview date's plane</button>
    <button class="alt" id="ttoday">Back to today</button>
    <button class="alt" id="treveal">Reveal now</button>
    <button class="alt" id="treset">Reset guesses</button>
  </div>
  <div class="legend">Test mode does not save progress. Reloading starts fresh.</div>
</div>

<div class="card"><div id="photoBox"></div><div class="credit" id="credit"></div></div>

<div class="card" id="play">
  <p class="count" id="count"></p>
  <div class="row">
    <input id="guess" autocomplete="off" placeholder="Type a plane name or ICAO code...">
    <button id="go">Guess</button>
    <div id="sugg" hidden></div>
  </div>
  <div id="msg"></div>
</div>

<div class="card scroll" id="board" hidden>
  <table><thead><tr><th>Aircraft</th><th>Brand</th><th>Year</th><th>Wingspan</th><th>Speed</th><th>Weight</th><th>Seats</th><th>Rarity</th></tr></thead>
  <tbody id="rows"></tbody></table>
  <div class="legend">&#9650; answer is higher &nbsp; &#9660; answer is lower &nbsp; &#10003; correct &nbsp; Brand: &#10003; same manufacturer, &#10007; different &nbsp; ? you guessed a plane with no data for that stat &nbsp; &ndash; answer has no data for that stat</div>
</div>

<div class="card" id="result" hidden></div>
</div>
<script>
const $=s=>document.querySelector(s);
const esc=s=>String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
let S,PLANES=[],guesses=[],over=false,selected=null,items=[],idx=-1;
const api=async p=>(await fetch(p)).json();
const key=()=>'skydle:'+S.date;

function renderCredit(){
  const el=$('#credit');
  if(!S.has_photo){el.innerHTML='';return;}
  const c=S.credit;
  if(c){
    let h='Photo: '+esc(c.photographer);
    if(c.license)h+=' &middot; '+esc(c.license);
    if(c.url)h+=' &middot; <a href="'+esc(c.url)+'" target="_blank" rel="noopener noreferrer">Source</a>';
    el.innerHTML=h;
  }else if(S.test_mode){
    el.innerHTML='<span class="warn">&#9888; No credit listed for this photo. Add a row to photos/credits.csv</span>';
  }else el.innerHTML='';
}
function renderPhoto(){
  renderCredit();
  const box=$('#photoBox');
  const none='<div class="nopic"><span>&#128247;</span>No picture available</div>';
  if(!S.has_photo){box.innerHTML=none;return;}
  box.innerHTML='<img alt="Mystery aircraft" src="/photo/today?t='+Date.now()+'">';
  box.querySelector('img').onerror=()=>{box.innerHTML=none;$('#credit').innerHTML='';};
}
function fmt(v,u){if(v===null)return '?';return (Number.isInteger(v)?v:+v.toFixed(3))+u;}
function cell(s){
  const m={correct:['ok','&#10003;'],higher:['hi','&#9650; higher'],lower:['lo','&#9660; lower'],
           unknown:['','?'],nodata:['','&ndash; no data']}[s.result];
  return `<td class="${m[0]}"><div>${fmt(s.value,s.unit)}</div><div class="a">${m[1]}</div></td>`;
}
function brandCell(b){
  if(!b)return '<td><div>?</div></td>';
  const m={correct:['ok','&#10003; correct'],wrong:['bad','&#10007; wrong'],unknown:['','?'],nodata:['','&ndash; no brand listed']}[b.result];
  return `<td class="${m[0]}"><div>${b.value?esc(b.value):'?'}</div><div class="a">${m[1]}</div></td>`;
}
function renderBoard(){
  $('#board').hidden=guesses.length===0;
  $('#rows').innerHTML=guesses.map(g=>`<tr><td class="nm">${esc(g.name)}<small>${esc(g.icao)}</small></td>${brandCell(g.brand)}${g.stats.map(cell).join('')}</tr>`).join('');
  $('#count').textContent=over?'Game over':`Guess ${guesses.length+1} of ${S.max_guesses}`;
  $('#play').style.display=over?'none':'';
}
function save(){if(!S.test_mode)localStorage.setItem(key(),JSON.stringify(guesses));}
async function finish(){
  over=true;renderBoard();
  const won=guesses.some(g=>g.correct);
  const r=await api('/api/reveal');
  const chips=(r.brand?`<span>Brand: ${esc(r.brand)}</span>`:'')+r.stats.map(s=>`<span>${s.label}: ${fmt(s.value,s.unit)}</span>`).join('');
  $('#result').hidden=false;
  $('#result').innerHTML=`<h2>${won?'&#127881; Correct in '+guesses.length+'/'+S.max_guesses+'!':'Out of guesses!'}</h2>
    <div>The aircraft was <b>${esc(r.name)}</b> (${esc(r.icao)}).</div>
    <div style="margin-top:8px">&#128214; <a href="${esc(r.wiki)}" target="_blank" rel="noopener">${esc(r.wiki)}</a></div>
    <div class="chips">${chips}</div>`;
}
function setMsg(t){$('#msg').textContent=t||'';}

/* ---- autocomplete ---- */
function refresh(){
  const t=$('#guess').value.trim().toLowerCase();selected=null;
  if(!t){items=[];idx=-1;draw();return;}
  const used=new Set(guesses.map(g=>g.icao));
  const sc=p=>{const n=p.name.toLowerCase(),i=p.icao.toLowerCase();
    if(i===t)return 0;if(n.startsWith(t))return 1;if(i.startsWith(t))return 2;if(n.includes(t))return 3;return 9;};
  items=PLANES.filter(p=>!used.has(p.icao)&&sc(p)<9).sort((a,b)=>sc(a)-sc(b)).slice(0,8);
  idx=items.length?0:-1;draw();
}
function draw(){
  const el=$('#sugg');el.hidden=!items.length;
  el.innerHTML=items.map((p,i)=>`<div class="${i===idx?'on':''}" data-i="${i}">${esc(p.name)}<small>${esc(p.icao)}</small></div>`).join('');
}
function pick(p){selected=p;$('#guess').value=p.name;items=[];draw();}
$('#sugg').addEventListener('mousedown',e=>{const d=e.target.closest('div[data-i]');if(d){e.preventDefault();pick(items[+d.dataset.i]);}});
$('#guess').addEventListener('input',()=>{setMsg('');refresh();});
$('#guess').addEventListener('keydown',e=>{
  if(e.key==='ArrowDown'&&items.length){idx=(idx+1)%items.length;draw();e.preventDefault();}
  else if(e.key==='ArrowUp'&&items.length){idx=(idx-1+items.length)%items.length;draw();e.preventDefault();}
  else if(e.key==='Enter'){submit();}
  else if(e.key==='Escape'){items=[];draw();}
});
$('#guess').addEventListener('blur',()=>setTimeout(()=>{items=[];draw();},120));
$('#go').onclick=submit;

async function submit(){
  if(over)return;
  const typed=$('#guess').value.trim().toLowerCase();
  const p=selected||items[idx]||PLANES.find(x=>x.icao.toLowerCase()===typed||x.name.toLowerCase()===typed);
  if(!p){setMsg('Pick a plane from the list.');return;}
  if(guesses.some(g=>g.icao===p.icao)){setMsg('You already guessed that one.');return;}
  const r=await api('/api/guess?icao='+encodeURIComponent(p.icao));
  if(r.error){setMsg(r.error);return;}
  guesses.push(r);save();
  $('#guess').value='';selected=null;items=[];draw();setMsg('');
  if(r.correct||guesses.length>=S.max_guesses)await finish();else renderBoard();
}

/* ---- test panel ---- */
async function setupTest(){
  $('#test').hidden=false;
  const tp=await api('/api/test/planes');
  $('#tsel').innerHTML=tp.map(p=>`<option value="${esc(p.icao)}">${esc(p.name)} (${esc(p.icao)}) - ${p.filled}/6 stats${p.eligible?'':' - NOT eligible'}</option>`).join('');
  const reload=async()=>{S=await api('/api/state');guesses=[];over=false;$('#result').hidden=true;
    $('#ans').textContent=S.answer.name+' ('+S.answer.icao+')';$('#date').textContent=S.date;renderPhoto();renderBoard();};
  $('#tset').onclick=async()=>{await api('/api/test/set?icao='+$('#tsel').value);reload();};
  $('#trand').onclick=async()=>{await api('/api/test/set?icao=random');reload();};
  $('#tdset').onclick=async()=>{if($('#tdate').value){await api('/api/test/date?d='+$('#tdate').value);reload();}};
  $('#ttoday').onclick=async()=>{await api('/api/test/clear');reload();};
  $('#treset').onclick=()=>{guesses=[];over=false;$('#result').hidden=true;renderBoard();};
  $('#treveal').onclick=()=>finish();
  $('#ans').textContent=S.answer.name+' ('+S.answer.icao+')';
}

(async function init(){
  S=await api('/api/state');PLANES=await api('/api/planes');
  $('#date').textContent=new Date(S.date+'T12:00:00').toLocaleDateString(undefined,{weekday:'long',year:'numeric',month:'long',day:'numeric'})
    +' - guess the aircraft in '+S.max_guesses+' tries';
  renderPhoto();
  if(S.test_mode)await setupTest();
  else{try{guesses=JSON.parse(localStorage.getItem(key())||'[]');}catch(e){guesses=[];}}
  renderBoard();
  if(guesses.some(g=>g.correct)||guesses.length>=S.max_guesses)await finish();
})();
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
def run_selftest(csv_path):
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))

    print("\nSKYDLE SELF-TEST\n")
    print("CSV")
    path, tried = resolve_csv(csv_path)
    check("CSV file found", path is not None, str(path) if path else "looked in: " + " | ".join(tried))
    if not path:
        print("\nCannot continue without the CSV.")
        return 1
    planes = load_planes(path)
    check("Planes loaded", len(planes) > 0, f"{len(planes)} planes")
    check("ICAO codes are unique", len({p['icao'] for p in planes}) == len(planes))
    check("Header row not loaded as a plane", not any(p["icao"].upper() == "ICAO" for p in planes))

    print("\nEligibility & daily pick")
    elig = eligible_planes(planes)
    check("Eligible planes exist", len(elig) > 0, f"{len(elig)} eligible (>= {MIN_FILLED_STATS} stats)")
    check("Every eligible plane has enough stats", all(filled_count(p) >= MIN_FILLED_STATS for p in elig))
    d = date(2026, 1, 15)
    check("Same date gives same plane", daily_answer(planes, d)["icao"] == daily_answer(planes, d)["icao"])
    picks = {daily_answer(planes, date(2026, 1, i))["icao"] for i in range(1, 29)}
    check("Different dates give variety", len(picks) > 5, f"{len(picks)} unique in 28 days")

    print("\nHigher / lower logic")
    check("answer bigger -> higher", compare(5, 10) == "higher")
    check("answer smaller -> lower", compare(10, 5) == "lower")
    check("equal -> correct", compare(7, 7) == "correct")
    check("guess missing -> unknown", compare(None, 7) == "unknown")
    check("answer missing -> nodata", compare(7, None) == "nodata")
    a, g = elig[0], elig[-1]
    j = judge(g, a)
    check("judge() returns all 6 categories", [s["label"] for s in j["stats"]] == [s[0] for s in STATS])
    check("judge() marks only the right plane correct", judge(a, a)["correct"] and not j["correct"])

    print("\nBrands")
    for nm, want in [("BOEING MQ-28 Ghost Bat", "BOEING"), ("AIRBUS A380-800", "AIRBUS"),
                     ("DE HAVILLAND CANADA Dash 8-Q400", "DE HAVILLAND CANADA"),
                     ("GRUMMAN AMERICAN AA-1", "GRUMMAN AMERICAN"), ("GRUMMAN TBF Avenger", "GRUMMAN"),
                     ("WACO D", "WACO"), ("G1 AVIATION G-1", "G1 AVIATION"), ("B & F TECHNIK FK-12 Comet", "B & F TECHNIK"),
                     ("K & S Cavalier", "K & S"), ("Drone", None), ("Balloon", None)]:
        check(f"brand of '{nm}'", extract_brand(nm) == want, f"got {extract_brand(nm)!r}")
    check("same brand -> correct", compare_brand("Boeing", "BOEING") == "correct")
    check("different brand -> wrong", compare_brand("AIRBUS", "BOEING") == "wrong")
    check("answer without brand -> nodata", compare_brand("BOEING", None) == "nodata")
    check("guess without brand -> unknown", compare_brand(None, "BOEING") == "unknown")
    check("judge() includes brand result", judge(a, a)["brand"]["result"] in ("correct", "nodata"))
    n_brands = len({p["brand"] for p in planes if p["brand"]})
    check("Brands extracted for the CSV", n_brands > 50,
          f"{n_brands} brands, {sum(1 for p in planes if not p['brand'])} planes with no brand")

    print("\nPhotos")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        check("No photo -> None", find_photo(tmpdir, "A388") is None)
        (tmpdir / "a388.JPG").write_bytes(b"fake-image")
        check("Photo found (case-insensitive)", find_photo(tmpdir, "A388") is not None)
        check("Other plane still has no photo", find_photo(tmpdir, "B738") is None)
        check("Missing photo folder doesn't crash", find_photo(tmpdir / "nope", "A388") is None)
        (tmpdir / "a388.JPG").unlink()

        print("\nPhoto credits")
        check("No credits.csv -> no credit", get_credit(tmpdir, "A388") is None)
        (tmpdir / "credits.csv").write_text(
            "icao,photographer,license,source_url\n"
            "A388,Jane Doe,CC BY-SA 4.0,https://example.com/photo\n"
            "ZZZ1,Sneaky,MIT,javascript:alert(1)\n"
            "ZZZ2,,CC0,\n", encoding="utf-8")
        cr = get_credit(tmpdir, "a388")
        check("Credit found (case-insensitive)", cr and cr["photographer"] == "Jane Doe" and cr["license"] == "CC BY-SA 4.0")
        check("Credit keeps a normal https link", cr and cr["url"] == "https://example.com/photo")
        check("Unsafe link (javascript:) is dropped", get_credit(tmpdir, "ZZZ1")["url"] == "")
        check("Row with no photographer counts as missing", get_credit(tmpdir, "ZZZ2") is None)
        check("Unlisted plane has no credit", get_credit(tmpdir, "B738") is None)
        ensure_photo_setup(tmpdir / "newphotos")
        check("ensure_photo_setup creates folder + credits.csv", (tmpdir / "newphotos" / "credits.csv").exists())
        (tmpdir / "credits.csv").unlink()

        print("\nWebsite (live server on a random port)")
        game = Game(planes, tmpdir)
        srv = make_server(game, 0)
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def get(p):
            try:
                with urllib.request.urlopen(base + p, timeout=10) as r:
                    return r.status, r.read(), r.headers.get("Content-Type", "")
            except urllib.error.HTTPError as e:
                return e.code, e.read(), ""

        code, body, _ = get("/")
        check("Home page loads", code == 200 and b"SKYDLE" in body)
        code, body, _ = get("/api/state")
        st = json.loads(body)
        ans = game.answer()
        check("State loads", code == 200 and st["max_guesses"] == MAX_GUESSES)
        check("State does NOT leak the answer", ans["name"].encode() not in body and "answer" not in st)
        code, body, _ = get("/api/planes")
        check("Plane list loads", code == 200 and len(json.loads(body)) == len(planes))
        code, body, _ = get("/api/guess?icao=" + urllib.parse.quote(ans["icao"]))
        check("Guessing the answer is marked correct", code == 200 and json.loads(body)["correct"])
        wrong = next(p for p in planes if p["icao"] != ans["icao"])
        code, body, _ = get("/api/guess?icao=" + urllib.parse.quote(wrong["icao"]))
        r = json.loads(body)
        check("Wrong guess is incorrect with 6 hints", code == 200 and not r["correct"] and len(r["stats"]) == 6)
        check("Guess response includes brand result", r["brand"]["result"] in ("correct", "wrong", "unknown", "nodata"))
        check("Unknown plane rejected", get("/api/guess?icao=NOPE")[0] == 400)
        code, _, _ = get("/photo/today")
        check("No photo -> /photo/today is 404 (page shows 'No picture')", code == 404 and st["has_photo"] is False)
        (tmpdir / f"{ans['icao']}.png").write_bytes(b"\x89PNG fake")
        code, body, ctype = get("/photo/today")
        check("With photo -> /photo/today serves it", code == 200 and ctype == "image/png")
        st2 = json.loads(get("/api/state")[1])
        check("State reports has_photo", st2["has_photo"] is True)
        check("Photo without a credits row -> credit is null", st2["credit"] is None)
        (tmpdir / "credits.csv").write_text(
            f"icao,photographer,license,source_url\n{ans['icao']},Jane Doe,CC BY 4.0,https://example.com/p\n",
            encoding="utf-8")
        st3 = json.loads(get("/api/state")[1])
        check("Credit appears in state once listed", st3["credit"] and st3["credit"]["photographer"] == "Jane Doe")
        code, body, _ = get("/api/reveal")
        rv = json.loads(body)
        check("Reveal returns name + Wikipedia link",
              rv["name"] == ans["name"] and rv["wiki"].startswith("https://en.wikipedia.org/"), rv["wiki"])
        check("Test endpoints blocked outside test mode", get("/api/test/planes")[0] == 404)
        srv.shutdown()

        game2 = Game(planes, tmpdir, test_mode=True)
        srv2 = make_server(game2, 0)
        base = f"http://127.0.0.1:{srv2.server_address[1]}"
        threading.Thread(target=srv2.serve_forever, daemon=True).start()
        print("\nTest mode")
        check("Test mode shows the answer", "answer" in json.loads(get("/api/state")[1]))
        target = wrong["icao"]
        get("/api/test/set?icao=" + urllib.parse.quote(target))
        check("Can force a specific answer", json.loads(get("/api/state")[1])["answer"]["icao"] == target)
        get("/api/test/set?icao=random")
        check("Random answer works", game2.test_answer is not None)
        get("/api/test/date?d=2026-03-01")
        check("Date preview matches daily pick",
              game2.answer()["icao"] == daily_answer(planes, date(2026, 3, 1))["icao"])
        check("Bad date rejected", get("/api/test/date?d=banana")[0] == 400)
        get("/api/test/clear")
        check("Clear returns to today's plane", game2.test_answer is None and game2.test_date is None)
        srv2.shutdown()

    passed, total = sum(results), len(results)
    print(f"\n{passed}/{total} checks passed" + ("  - ALL GOOD" if passed == total else "  - SEE FAILURES ABOVE"))
    return 0 if passed == total else 1


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def ask_mode():
    """Startup menu. Returns 'daily', 'test', 'selftest', or None to quit."""
    print("\n==============================")
    print("   SKYDLE - choose a mode")
    print("==============================")
    print("  1) Daily game")
    print("  2) Test mode")
    print("  3) Run self-test")
    print("  q) Quit\n")
    while True:
        try:
            c = input("Type 1, 2, 3 or q and press Enter: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return None
        if c in ("1", "d", "daily"):
            return "daily"
        if c in ("2", "t", "test"):
            return "test"
        if c in ("3", "s", "selftest"):
            return "selftest"
        if c in ("q", "quit", "exit"):
            return None
        print("  Please type 1, 2, 3 or q.")


def main():
    try:  # keep Windows consoles from choking on odd characters
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Skydle - daily aircraft guessing game")
    ap.add_argument("--csv", help="path to the Skycards CSV")
    ap.add_argument("--photos", help="photo folder (default: photos next to this script)")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--daily", action="store_true", help="skip the menu and start the daily game")
    ap.add_argument("--testmode", action="store_true", help="skip the menu and start test mode")
    ap.add_argument("--selftest", action="store_true", help="run automated checks and exit")
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    args = ap.parse_args()

    # No mode given on the command line: ask at startup
    if not (args.daily or args.testmode or args.selftest) and sys.stdin.isatty():
        choice = ask_mode()
        if choice is None:
            return
        args.testmode = choice == "test"
        args.selftest = choice == "selftest"

    if args.selftest:
        sys.exit(run_selftest(args.csv))

    path, tried = resolve_csv(args.csv)
    if not path:
        print("Could not find the CSV. I looked here:")
        for t in tried:
            print("   -", t)
        print('\nFix: pass the path, e.g.  python skydle.py --csv "C:\\path\\to\\file.csv"')
        sys.exit(1)

    planes = load_planes(path)
    photo_dir = Path(args.photos) if args.photos else PHOTO_DIR
    ensure_photo_setup(photo_dir)
    game = Game(planes, photo_dir, test_mode=args.testmode)
    try:
        srv = make_server(game, args.port)
    except OSError:
        sys.exit(f"Port {args.port} is busy. Try:  python skydle.py --port {args.port + 1}")

    url = f"http://localhost:{args.port}"
    print(f"CSV:      {path}  ({len(planes)} planes, {len(eligible_planes(planes))} eligible)")
    print(f"Photos:   {photo_dir}   (credits go in {photo_dir / 'credits.csv'})")
    print(f"Mode:     {'TEST MODE' if args.testmode else 'normal'}")
    print(f"Game at:  {url}   (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nBye!")


def _pause():
    """When double-clicked on Windows, keep the window open so messages can be read."""
    if sys.platform.startswith("win") and len(sys.argv) == 1 and sys.stdin.isatty():
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass


if __name__ == "__main__":
    code = 0
    try:
        main()
    except SystemExit as e:
        code = e.code
        if isinstance(code, str):
            print(code)
            code = 1
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    _pause()
    sys.exit(code)
