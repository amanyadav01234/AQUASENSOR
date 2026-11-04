"""AquaSense server - zero third-party dependencies (Python standard library only).

Run:  python server.py        ->  http://localhost:8000
Env:  PORT, AQUA_SECRET, AQUA_DB, AQUA_TOKEN_TTL (seconds), AQUA_SIMULATE=1 (live demo data)
"""
import csv, hashlib, hmac, io, json, mimetypes, os, re, secrets, sqlite3, threading, time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
WEB = ROOT / "web"
DB = os.environ.get("AQUA_DB", str(ROOT / "aquasense.db"))
SECRET = os.environ.get("AQUA_SECRET", "dev-secret-change-me").encode()
TOKEN_TTL = int(os.environ.get("AQUA_TOKEN_TTL", 3600))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_BODY = 64 * 1024
SIM_STEP = timedelta(minutes=30)  # each simulated reading advances the device clock by 30 min
LOGIN_MAX_FAILS, LOGIN_WINDOW = 5, 300  # 5 failed logins per 5 minutes per (ip, email)
PUMP_ON_BELOW = 30  # auto mode: pump runs while level is under this

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# ---------------------------------------------------------------- database
@contextmanager
def db():
    """Connection that commits on success, rolls back on error and always closes."""
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise
    finally:
        c.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL, salt BLOB NOT NULL, pw BLOB NOT NULL, created_at TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY, name TEXT NOT NULL, location TEXT NOT NULL,
            capacity_litres INTEGER NOT NULL, low_threshold INTEGER NOT NULL DEFAULT 20,
            pump_mode TEXT NOT NULL DEFAULT 'auto', online INTEGER NOT NULL DEFAULT 1, last_seen TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS readings(id INTEGER PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(id),
            timestamp TEXT NOT NULL, level_percent REAL NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_readings ON readings(device_id, timestamp);
        """)
        if c.execute("SELECT COUNT(*) FROM devices").fetchone()[0] == 0:
            seed(c)


def seed(c):
    """tank-1 = the assignment's mock data. tank-2/3 are extra demo devices."""
    now = datetime.now(timezone.utc).isoformat()
    c.executemany("INSERT INTO devices VALUES(?,?,?,?,?,?,?,?)", [
        ("tank-1", "Rooftop Tank A", "Block A, Roof", 5000, 20, "auto", 1, now),
        ("tank-2", "Basement Sump", "Block B, Basement", 8000, 20, "auto", 1, now),
        ("tank-3", "Garden Reservoir", "North Lawn", 3000, 20, "auto", 0, "2026-11-02T14:05:00+00:00"),
    ])
    mock = json.loads((ROOT / "data" / "readings.json").read_text())
    rows = [("tank-1", r["timestamp"], r["level_percent"]) for r in mock]
    t, lvl = datetime(2026, 11, 1, 6), 88.0  # tank-2: sawtooth - drains, then refills
    while t <= datetime(2026, 11, 2, 20):
        rows.append(("tank-2", t.isoformat(), round(lvl, 1)))
        lvl -= (6, 9, 7)[int(t.hour / 2) % 3]
        if lvl < 22: lvl = 92.0
        t += timedelta(hours=2)
    t, lvl = datetime(2026, 11, 1, 6), 71.0  # tank-3: offline since 14:00 on Nov 2
    while t <= datetime(2026, 11, 2, 14):
        rows.append(("tank-3", t.isoformat(), round(lvl, 1)))
        lvl = max(lvl - 2.4, 5)
        t += timedelta(hours=2)
    c.executemany("INSERT INTO readings(device_id,timestamp,level_percent) VALUES(?,?,?)", rows)


# ---------------------------------------------------------------- auth
def hash_pw(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)


def make_token(uid):
    body = f"{uid}.{int(time.time()) + TOKEN_TTL}"
    return f"{body}.{hmac.new(SECRET, body.encode(), 'sha256').hexdigest()}"


def verify_token(tok):
    try:
        uid, exp, sig = tok.split(".")
        good = hmac.new(SECRET, f"{uid}.{exp}".encode(), "sha256").hexdigest()
        return int(uid) if hmac.compare_digest(sig, good) and int(exp) > time.time() else None
    except (ValueError, AttributeError):
        return None


_fails, _lock = {}, threading.Lock()


def throttled(key):
    """Seconds until another login attempt is allowed (0 = allowed)."""
    now = time.time()
    with _lock:
        hits = [t for t in _fails.get(key, []) if now - t < LOGIN_WINDOW]
        _fails[key] = hits
        return int(LOGIN_WINDOW - (now - hits[0])) + 1 if len(hits) >= LOGIN_MAX_FAILS else 0


def record_fail(key):
    with _lock:
        _fails.setdefault(key, []).append(time.time())


def clear_fails(key):
    with _lock:
        _fails.pop(key, None)


def validate_password(pw):
    if len(pw) < 8: return "Password must be at least 8 characters"
    if not (re.search(r"[A-Za-z]", pw) and re.search(r"\d", pw)): return "Password must include a letter and a number"
    return None


# ---------------------------------------------------------------- domain logic
def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)


def get_device(c, did):
    return c.execute("SELECT * FROM devices WHERE id=?", (did,)).fetchone()


def get_readings(c, did, rng="all"):
    rows = [dict(r) for r in c.execute(
        "SELECT timestamp, level_percent FROM readings WHERE device_id=? ORDER BY timestamp", (did,))]
    if rows and rng == "today":  # "today" = the calendar day of the LATEST reading, not the viewer's clock
        day = rows[-1]["timestamp"][:10]
        rows = [r for r in rows if r["timestamp"][:10] == day]
    return rows


def pump_state(dev, level):
    if dev["pump_mode"] in ("on", "off"): return dev["pump_mode"]
    return "on" if level is not None and level < PUMP_ON_BELOW else "off"


def summarize(dev, rows):
    thr = dev["low_threshold"]
    out = {"id": dev["id"], "name": dev["name"], "location": dev["location"], "capacity_litres": dev["capacity_litres"],
           "low_threshold": thr, "pump_mode": dev["pump_mode"], "online": bool(dev["online"]),
           "last_seen": dev["last_seen"], "current": None}
    if not rows:
        return {**out, "pump_state": "off", "today": None}
    cur = rows[-1]
    today = [r for r in rows if r["timestamp"][:10] == cur["timestamp"][:10]]
    mn = min(today, key=lambda r: r["level_percent"])  # ties -> earliest
    mx = max(today, key=lambda r: r["level_percent"])
    out.update(current=cur, pump_state=pump_state(dev, cur["level_percent"]), is_low=cur["level_percent"] < thr,
               litres=round(dev["capacity_litres"] * cur["level_percent"] / 100),
               today={"date": cur["timestamp"][:10], "min": mn, "max": mx, "readings": len(today),
                      "low_count": sum(r["level_percent"] < thr for r in today)})
    out["trend_per_hour"], out["hours_to_low"] = None, None
    if len(rows) >= 2:
        hrs = (parse_ts(rows[-1]["timestamp"]) - parse_ts(rows[-2]["timestamp"])).total_seconds() / 3600
        if hrs > 0:
            slope = (rows[-1]["level_percent"] - rows[-2]["level_percent"]) / hrs
            out["trend_per_hour"] = round(slope, 1)
            if slope < 0 and cur["level_percent"] >= thr:
                out["hours_to_low"] = round((cur["level_percent"] - thr) / -slope, 1)
    return out


def alert_episodes(dev, rows):
    """Group consecutive readings below the threshold into episodes (strictly below: 20% exactly is OK)."""
    thr, eps, cur = dev["low_threshold"], [], None
    for r in rows:
        if r["level_percent"] < thr:
            if cur is None:
                cur = {"device_id": dev["id"], "device_name": dev["name"], "started": r["timestamp"], "lowest": r["level_percent"],
                       "lowest_at": r["timestamp"], "ended": None, "readings": 0, "threshold": thr}
            cur["readings"] += 1
            if r["level_percent"] < cur["lowest"]: cur["lowest"], cur["lowest_at"] = r["level_percent"], r["timestamp"]
        elif cur:
            cur["ended"] = r["timestamp"]; eps.append(cur); cur = None
    if cur: eps.append(cur)
    for e in eps:
        e["active"] = e["ended"] is None
        if e["ended"]:
            e["duration_minutes"] = int((parse_ts(e["ended"]) - parse_ts(e["started"])).total_seconds() // 60)
    return eps


def add_reading(c, dev, level, ts=None):
    if ts is None:  # continue the device's own timeline (demo clock)
        last = c.execute("SELECT MAX(timestamp) FROM readings WHERE device_id=?", (dev["id"],)).fetchone()[0]
        ts = (parse_ts(last) + SIM_STEP).isoformat() if last else datetime.now().replace(microsecond=0).isoformat()
    c.execute("INSERT INTO readings(device_id,timestamp,level_percent) VALUES(?,?,?)", (dev["id"], ts, level))
    c.execute("UPDATE devices SET last_seen=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), dev["id"]))
    return {"timestamp": ts, "level_percent": level}


def valid_level(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and 0 <= v <= 100


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "AquaSense/2.0"

    def log_message(self, fmt, *args):
        if os.environ.get("AQUA_QUIET") != "1": super().log_message(fmt, *args)

    def _headers(self, code, ctype, length, extra=()):
        self.send_response(code)
        for k, v in (("Content-Type", ctype), ("Content-Length", str(length)), ("X-Content-Type-Options", "nosniff"),
                     ("X-Frame-Options", "DENY"), ("Referrer-Policy", "no-referrer"), ("Content-Security-Policy", CSP), *extra):
            self.send_header(k, v)
        self.end_headers()

    def send_json(self, code, obj, extra=()):
        raw = json.dumps(obj).encode()
        self._headers(code, "application/json", len(raw), (("Cache-Control", "no-store"), *extra))
        self.wfile.write(raw)

    def body(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > MAX_BODY: return None
            data = json.loads(self.rfile.read(n) or b"{}")
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def user(self):
        h = self.headers.get("Authorization", "")
        uid = verify_token(h[7:]) if h.startswith("Bearer ") else None
        if uid is None: return None
        with db() as c:
            return c.execute("SELECT id,name,email,created_at FROM users WHERE id=?", (uid,)).fetchone()

    # ---- GET
    def do_GET(self):
        path = self.path.split("?")[0]
        query = dict(p.split("=", 1) for p in self.path.partition("?")[2].split("&") if "=" in p)
        if path == "/api/health":
            return self.send_json(200, {"status": "ok", "time": datetime.now(timezone.utc).isoformat()})
        if path.startswith(("/api/", "/device/")):
            u = self.user()
            if not u: return self.send_json(401, {"error": "Authentication required"})
            return self.api_get(u, path, query)
        f = (WEB / ("index.html" if path == "/" else path.lstrip("/"))).resolve()
        if not f.is_relative_to(WEB.resolve()) or not f.is_file():
            return self.send_json(404, {"error": "Not found"})
        raw = f.read_bytes()
        self._headers(200, mimetypes.guess_type(f.name)[0] or "application/octet-stream", len(raw), (("Cache-Control", "no-cache"),))
        self.wfile.write(raw)

    def api_get(self, u, path, query):
        with db() as c:
            if path == "/api/me":
                return self.send_json(200, {"name": u["name"], "email": u["email"], "member_since": u["created_at"]})
            if path == "/api/devices":
                out = [summarize(d, get_readings(c, d["id"])) for d in c.execute("SELECT * FROM devices ORDER BY id")]
                for o in out: o["spark"] = [r["level_percent"] for r in get_readings(c, o["id"])[-12:]]
                return self.send_json(200, out)
            if path == "/api/alerts":
                eps = []
                for d in c.execute("SELECT * FROM devices"): eps += alert_episodes(d, get_readings(c, d["id"]))
                return self.send_json(200, sorted(eps, key=lambda e: e["started"], reverse=True))
            if path == "/api/readings":  # legacy alias for tank-1
                return self.send_json(200, get_readings(c, "tank-1"))
            m = re.fullmatch(r"/device/([\w-]+)/status", path)
            if m:
                d = get_device(c, m.group(1))
                if not d: return self.send_json(404, {"error": "Device not found"})
                if not d["online"]:
                    return self.send_json(503, {"error": "Device offline", "last_seen": d["last_seen"]})
                rows = get_readings(c, d["id"])
                lvl = rows[-1]["level_percent"] if rows else 0
                return self.send_json(200, {"level_percent": lvl, "pump_state": pump_state(d, lvl),
                                            "last_seen": datetime.now(timezone.utc).isoformat()})
            m = re.fullmatch(r"/api/devices/([\w-]+)(?:/(readings|export\.csv))?", path)
            if m:
                d = get_device(c, m.group(1))
                if not d: return self.send_json(404, {"error": "Device not found"})
                if m.group(2) == "readings":
                    rng = query.get("range", "all")
                    if rng not in ("today", "all"): return self.send_json(400, {"error": "range must be today or all"})
                    return self.send_json(200, get_readings(c, d["id"], rng))
                if m.group(2) == "export.csv":
                    buf = io.StringIO(); w = csv.writer(buf); w.writerow(["timestamp", "level_percent"])
                    w.writerows((r["timestamp"], r["level_percent"]) for r in get_readings(c, d["id"]))
                    raw = buf.getvalue().encode()
                    self._headers(200, "text/csv", len(raw), (("Content-Disposition", f'attachment; filename="{d["id"]}-readings.csv"'), ("Cache-Control", "no-store")))
                    return self.wfile.write(raw)
                rows = get_readings(c, d["id"])
                return self.send_json(200, {**summarize(d, rows), "episodes": alert_episodes(d, rows)})
        self.send_json(404, {"error": "Not found"})

    # ---- POST
    def do_POST(self):
        path, data = self.path.split("?")[0], self.body()
        if data is None: return self.send_json(400, {"error": "Invalid JSON"})
        if path == "/api/signup": return self.signup(data)
        if path == "/login": return self.login(data)
        u = self.user()
        if not u: return self.send_json(401, {"error": "Authentication required"})
        if path == "/api/refresh": return self.send_json(200, {"token": make_token(u["id"]), "name": u["name"]})
        if path == "/api/me/password": return self.change_password(u, data)
        m = re.fullmatch(r"/api/devices/([\w-]+)/(readings|pump|settings)", path) or \
            (re.fullmatch(r"/api/(readings)", path) and ("tank-1", "readings"))
        if not m: return self.send_json(404, {"error": "Not found"})
        did, action = (m if isinstance(m, tuple) else (m.group(1), m.group(2)))
        with db() as c:
            d = get_device(c, did)
            if not d: return self.send_json(404, {"error": "Device not found"})
            if action == "readings":
                if not valid_level(data.get("level_percent")): return self.send_json(400, {"error": "level_percent must be 0-100"})
                out = add_reading(c, d, data["level_percent"]); c.commit()
                return self.send_json(201, out)
            if action == "pump":
                if data.get("mode") not in ("auto", "on", "off"): return self.send_json(400, {"error": "mode must be auto, on or off"})
                c.execute("UPDATE devices SET pump_mode=? WHERE id=?", (data["mode"], did)); c.commit()
                return self.send_json(200, {"pump_mode": data["mode"]})
            name, thr = str(data.get("name", d["name"])).strip(), data.get("low_threshold", d["low_threshold"])
            if not 2 <= len(name) <= 40: return self.send_json(400, {"error": "Name must be 2-40 characters"})
            if isinstance(thr, bool) or not isinstance(thr, int) or not 5 <= thr <= 60:
                return self.send_json(400, {"error": "Alert threshold must be a whole number from 5 to 60"})
            c.execute("UPDATE devices SET name=?, low_threshold=? WHERE id=?", (name, thr, did)); c.commit()
            return self.send_json(200, {"name": name, "low_threshold": thr})

    def signup(self, data):
        name, email, pw = str(data.get("name", "")).strip(), str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
        if not 2 <= len(name) <= 60: return self.send_json(400, {"error": "Name must be 2-60 characters"})
        if not EMAIL_RE.match(email): return self.send_json(400, {"error": "Enter a valid email"})
        err = validate_password(pw)
        if err: return self.send_json(400, {"error": err})
        salt = secrets.token_bytes(16)
        try:
            with db() as c:
                cur = c.execute("INSERT INTO users(name,email,salt,pw,created_at) VALUES(?,?,?,?,?)",
                                (name, email, salt, hash_pw(pw, salt), datetime.now(timezone.utc).isoformat()))
        except sqlite3.IntegrityError:
            return self.send_json(409, {"error": "An account with this email already exists"})
        self.send_json(201, {"token": make_token(cur.lastrowid), "name": name})

    def login(self, data):
        email, pw = str(data.get("email", "")).strip().lower(), str(data.get("password", ""))
        key = f"{self.client_address[0]}|{email}"
        wait = throttled(key)
        if wait: return self.send_json(429, {"error": f"Too many attempts. Try again in {wait // 60 + 1} min"}, (("Retry-After", str(wait)),))
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        ok = row and hmac.compare_digest(hash_pw(pw, row["salt"]), row["pw"])
        if not ok:  # same message for unknown email / wrong password (no user enumeration)
            record_fail(key)
            return self.send_json(401, {"error": "Invalid email or password"})
        clear_fails(key)
        self.send_json(200, {"token": make_token(row["id"]), "name": row["name"]})

    def change_password(self, u, data):
        cur_pw, new_pw = str(data.get("current_password", "")), str(data.get("new_password", ""))
        with db() as c:
            row = c.execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
            if not hmac.compare_digest(hash_pw(cur_pw, row["salt"]), row["pw"]):
                return self.send_json(403, {"error": "Current password is incorrect"})
            err = validate_password(new_pw)
            if err: return self.send_json(400, {"error": err})
            salt = secrets.token_bytes(16)
            c.execute("UPDATE users SET salt=?, pw=? WHERE id=?", (salt, hash_pw(new_pw, salt), u["id"])); c.commit()
        self.send_json(200, {"ok": True})


def simulator(stop):
    """Optional live demo: every few seconds, tank-2 gets a new reading."""
    import random
    while not stop.wait(5):
        with db() as c:
            d = get_device(c, "tank-2"); rows = get_readings(c, "tank-2")
            lvl = rows[-1]["level_percent"] + random.uniform(-6, 3)
            add_reading(c, d, round(min(100, max(3, lvl)), 1))


def make_server(port=0, host="127.0.0.1"):
    init_db()
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    srv = make_server(port, "0.0.0.0")
    if os.environ.get("AQUA_SIMULATE") == "1":
        threading.Thread(target=simulator, args=(threading.Event(),), daemon=True).start()
    if SECRET == b"dev-secret-change-me": print("WARNING: using the default AQUA_SECRET - set your own for real use")
    print(f"AquaSense running at http://localhost:{port}")
    try: srv.serve_forever()
    except KeyboardInterrupt: print("\nbye")
