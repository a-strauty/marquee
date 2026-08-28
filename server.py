#!/usr/bin/env python3
"""Backend for the Wisecoco 3840x1100 touch dashboard.

Serves the static page plus a small JSON API bridging:
  - playerctl  -> MPRIS media control (spotify, firefox, ...)
  - pactl      -> PipeWire microphone mute/volume/source switching
  - nmcli      -> NetworkManager VPN profiles
  - /proc + sensors -> CPU, RAM, temps, fans, disks, network
  - wttr.in    -> weather (optional, outbound HTTPS)

Stdlib only. Binds to loopback.
"""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import gpu
import spotify

HERE = Path(__file__).resolve().parent
STATIC = HERE / "static"

HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8770"))
NET_IFACE = os.environ.get("DASH_IFACE", "eno2")
DISKS = [d for d in os.environ.get("DASH_DISKS", "/,/mnt/storage,/mnt/share").split(",") if d]
WEATHER_ON = os.environ.get("DASH_WEATHER", "1") != "0"
WEATHER_LOC = os.environ.get("DASH_WEATHER_LOC", "")  # blank = geolocate by IP
UNITS_F = os.environ.get("DASH_UNITS", "F").upper() == "F"

# --- burn-in mitigation -------------------------------------------------
# The panel is an LCD, so the risk is image retention rather than true OLED
# burn-in - but a dashboard holds the same layout for months, so it's worth
# shifting the pixels around and easing off the luminance overnight.
SHIFT_SECONDS = int(os.environ.get("DASH_SHIFT_SECONDS", "90"))
SHIFT_PX = int(os.environ.get("DASH_SHIFT_PX", "10"))
NIGHT_START = int(os.environ.get("DASH_NIGHT_START", "22"))
NIGHT_END = int(os.environ.get("DASH_NIGHT_END", "7"))
NIGHT_DIM = float(os.environ.get("DASH_NIGHT_DIM", "0.55"))

SEP = "\x1f"
MEDIA_FMT = SEP.join([
    "{{status}}", "{{artist}}", "{{title}}", "{{album}}",
    "{{mpris:artUrl}}", "{{mpris:length}}",
])


def run(cmd, timeout=2.5):
    """Run a command, return stripped stdout, or '' on any failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception:
        return ""
    return p.stdout.strip() if p.returncode == 0 else ""


# ---------------------------------------------------------------- system

def read_cpu_times():
    with open("/proc/stat") as f:
        vals = [int(x) for x in f.readline().split()[1:]]
    return sum(vals), vals[3] + vals[4]  # total, idle+iowait


def read_mem():
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, _, v = line.partition(":")
            info[k] = int(v.split()[0])
    total = info["MemTotal"] * 1024
    avail = info.get("MemAvailable", info["MemFree"]) * 1024
    return total, total - avail


def read_net(iface):
    try:
        with open("/proc/net/dev") as f:
            for line in f:
                name, _, rest = line.partition(":")
                if name.strip() == iface:
                    f_ = rest.split()
                    return int(f_[0]), int(f_[8])  # rx, tx bytes
    except Exception:
        pass
    return 0, 0


def read_sensors():
    out = run(["sensors", "-j"], timeout=3)
    res = {"cpu_temp": None, "nvme_temp": None, "fans": []}
    if not out:
        return res
    try:
        data = json.loads(out)
    except Exception:
        return res
    for feats in data.values():
        if not isinstance(feats, dict):
            continue
        for name, vals in feats.items():
            if not isinstance(vals, dict):
                continue
            inp = next((v for k, v in vals.items()
                        if k.endswith("_input") and isinstance(v, (int, float))), None)
            if inp is None:
                continue
            low = name.lower()
            if "package id" in low or low.startswith("tctl"):
                res["cpu_temp"] = round(inp, 1)
            elif "composite" in low:
                res["nvme_temp"] = round(inp, 1)
            elif low.startswith("fan") and inp > 0:
                res["fans"].append(int(inp))
    return res


def read_disks():
    out = []
    for path in DISKS:
        try:
            st = os.statvfs(path)
        except Exception:
            continue
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            continue
        out.append({
            "path": path,
            "total": total,
            "used": total - free,
            "pct": round((total - free) / total * 100, 1),
        })
    return out


def read_load_uptime():
    with open("/proc/loadavg") as f:
        la = f.read().split()[:3]
    with open("/proc/uptime") as f:
        up = float(f.read().split()[0])
    return [float(x) for x in la], up


# ---------------------------------------------------------------- media

def list_players():
    out = run(["playerctl", "-l"])
    return [p for p in out.splitlines() if p.strip()]


def read_media(preferred=None):
    players = list_players()
    if not players:
        return {"players": [], "player": None, "status": "Stopped"}

    chosen = preferred if preferred in players else None
    if not chosen:
        for p in players:
            if run(["playerctl", "-p", p, "status"]) == "Playing":
                chosen = p
                break
    if not chosen:
        chosen = players[0]

    raw = run(["playerctl", "-p", chosen, "metadata", "--format", MEDIA_FMT])
    parts = raw.split(SEP) if raw else []
    parts += [""] * (6 - len(parts))
    status, artist, title, album, art, length = parts[:6]

    try:
        length_s = int(length) / 1_000_000 if length else 0.0
    except ValueError:
        length_s = 0.0
    try:
        pos_s = float(run(["playerctl", "-p", chosen, "position"]) or 0)
    except ValueError:
        pos_s = 0.0
    vol_raw = run(["playerctl", "-p", chosen, "volume"])
    try:
        vol = round(float(vol_raw) * 100) if vol_raw else None
    except ValueError:
        vol = None

    return {
        "players": players,
        "player": chosen,
        "status": status or "Stopped",
        "artist": artist,
        "title": title,
        "album": album,
        "art": art,
        "length": length_s,
        "position": pos_s,
        "volume": vol,
    }


# ---------------------------------------------------------------- mic

def read_mic():
    default = run(["pactl", "get-default-source"])
    muted = "yes" in run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"]).lower()
    m = re.search(r"(\d+)%", run(["pactl", "get-source-volume", "@DEFAULT_SOURCE@"]))
    vol = int(m.group(1)) if m else None

    sources, name, desc = [], None, None
    for line in run(["pactl", "list", "sources"]).splitlines():
        line = line.strip()
        if line.startswith("Name:"):
            name = line[5:].strip()
        elif line.startswith("Description:"):
            desc = line[12:].strip()
            if name and not name.endswith(".monitor"):
                sources.append({"name": name, "desc": desc, "default": name == default})
            name = desc = None

    label = next((s["desc"] for s in sources if s["default"]), default or "unknown")
    return {"muted": muted, "volume": vol, "source": default,
            "label": label, "sources": sources}


# ---------------------------------------------------------------- vpn

def read_vpn():
    profiles, active = [], None
    for line in run(["nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show"]).splitlines():
        # NAME may contain escaped colons; split the two known trailing fields.
        head, _, state = line.rpartition(":")
        name, _, typ = head.rpartition(":")
        if typ not in ("vpn", "wireguard"):
            continue
        name = name.replace("\\:", ":")
        up = state == "activated"
        profiles.append({"name": name, "type": typ, "up": up})
        if up:
            active = name

    ip = None
    if active:
        for line in run(["ip", "-o", "-4", "addr", "show"]).splitlines():
            if any(t in line for t in (" tun", " wg", " ppp")):
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", line)
                if m:
                    ip = m.group(1)
                    break
    return {"profiles": profiles, "active": active, "ip": ip}


# ---------------------------------------------------------------- weather

def fetch_weather():
    if not WEATHER_ON:
        return None
    url = "https://wttr.in/{}?format=j1".format(urllib.parse.quote(WEATHER_LOC))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
        cur = data["current_condition"][0]
        area = data.get("nearest_area", [{}])[0]
        today = data.get("weather", [{}])[0]
        place = ""
        for key in ("areaName", "region"):
            vals = area.get(key) or []
            if vals and vals[0].get("value"):
                place = vals[0]["value"]
                break
        return {
            "temp": int(cur["temp_F"] if UNITS_F else cur["temp_C"]),
            "feels": int(cur["FeelsLikeF"] if UNITS_F else cur["FeelsLikeC"]),
            "desc": cur["weatherDesc"][0]["value"],
            "humidity": int(cur["humidity"]),
            "wind": int(cur["windspeedMiles"] if UNITS_F else cur["windspeedKmph"]),
            "hi": int(today.get("maxtempF" if UNITS_F else "maxtempC", 0)),
            "lo": int(today.get("mintempF" if UNITS_F else "mintempC", 0)),
            "unit": "F" if UNITS_F else "C",
            "place": place,
        }
    except Exception:
        return None


# ---------------------------------------------------------------- sampler

STATE = {
    "media": {}, "mic": {}, "vpn": {}, "system": {}, "weather": None, "ts": 0,
    "config": {
        "shift_seconds": SHIFT_SECONDS,
        "shift_px": SHIFT_PX,
        "night_start": NIGHT_START,
        "night_end": NIGHT_END,
        "night_dim": NIGHT_DIM,
    },
}
LOCK = threading.Lock()
PREFERRED_PLAYER = {"name": None}


def sampler():
    prev_cpu = read_cpu_times()
    prev_net = read_net(NET_IFACE)
    prev_t = time.monotonic()
    sensors = read_sensors()
    gpu_stats = gpu.stats()
    disks = read_disks()
    weather = fetch_weather()
    tick = 0

    while True:
        time.sleep(1.0)
        tick += 1
        now = time.monotonic()
        dt = max(now - prev_t, 1e-6)

        cpu = read_cpu_times()
        d_total = cpu[0] - prev_cpu[0]
        d_idle = cpu[1] - prev_cpu[1]
        cpu_pct = round(max(0.0, (1 - d_idle / d_total) * 100), 1) if d_total > 0 else 0.0
        prev_cpu = cpu

        net = read_net(NET_IFACE)
        rx_rate = max(0, net[0] - prev_net[0]) / dt
        tx_rate = max(0, net[1] - prev_net[1]) / dt
        prev_net = net
        prev_t = now

        if tick % 2 == 0:
            # fdinfo walk is cheap but not free; 2s also gives a steadier
            # utilisation figure than a 1s window.
            gpu_stats = gpu.stats()
        if tick % 5 == 0:
            sensors = read_sensors()
        if tick % 30 == 0:
            disks = read_disks()
        if tick % 900 == 0:
            weather = fetch_weather()

        mem_total, mem_used = read_mem()
        load, uptime = read_load_uptime()

        media = read_media(PREFERRED_PLAYER["name"])
        mic = read_mic()
        vpn = read_vpn()

        with LOCK:
            STATE.update({
                "media": media,
                "mic": mic,
                "vpn": vpn,
                "weather": weather,
                "ts": time.time(),
                "system": {
                    "cpu": cpu_pct,
                    "mem_total": mem_total,
                    "mem_used": mem_used,
                    "mem_pct": round(mem_used / mem_total * 100, 1),
                    "cpu_temp": sensors["cpu_temp"],
                    "nvme_temp": sensors["nvme_temp"],
                    "fans": sensors["fans"],
                    "gpu": gpu_stats,
                    "disks": disks,
                    "net": {"iface": NET_IFACE, "rx": rx_rate, "tx": tx_rate},
                    "load": load,
                    "uptime": uptime,
                },
            })


# ---------------------------------------------------------------- art proxy

ART_CACHE = {}


def fetch_art(url):
    if url in ART_CACHE:
        return ART_CACHE[url]
    if url.startswith("file://"):
        p = urllib.parse.unquote(urllib.parse.urlparse(url).path)
        blob = (Path(p).read_bytes(), "image/jpeg")
    elif url.startswith(("http://", "https://")):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            blob = (r.read(), r.headers.get("Content-Type", "image/jpeg"))
    else:
        raise ValueError("unsupported art url")
    if len(ART_CACHE) > 40:
        ART_CACHE.clear()
    ART_CACHE[url] = blob
    return blob


# ---------------------------------------------------------------- http

CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css",
                 ".js": "text/javascript", ".svg": "image/svg+xml"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass  # keep the kiosk journal quiet

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        if u.path == "/api/state":
            with LOCK:
                return self._json(STATE)

        if u.path == "/api/spotify/status":
            return self._json(spotify.status())

        if u.path == "/api/spotify/library":
            try:
                return self._json(spotify.fetch_library(force="force" in q))
            except Exception as e:
                return self._json({"error": str(e)}, 503)

        if u.path == "/api/spotify/login":
            try:
                url = spotify.begin_auth(self._redirect_uri())
            except Exception as e:
                return self._send(400, str(e).encode(), "text/plain")
            self.send_response(302)
            self.send_header("Location", url)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if u.path == "/callback":
            err = (q.get("error") or [""])[0]
            if err:
                return self._send(400, self._page("Authorization denied", err), "text/html; charset=utf-8")
            try:
                spotify.finish_auth((q.get("code") or [""])[0],
                                    (q.get("state") or [""])[0],
                                    self._redirect_uri())
            except Exception as e:
                return self._send(400, self._page("Authorization failed", str(e)), "text/html; charset=utf-8")
            return self._send(200, self._page("Spotify connected",
                                              "Your library will appear on the panel. You can close this tab."),
                              "text/html; charset=utf-8")

        if u.path == "/api/art":
            url = (q.get("u") or [""])[0]
            if not url:
                return self._send(404, b"no url", "text/plain")
            try:
                blob, ctype = fetch_art(url)
            except Exception:
                return self._send(404, b"no art", "text/plain")
            return self._send(200, blob, ctype)

        rel = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        target = (STATIC / rel).resolve()
        if not str(target).startswith(str(STATIC.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain")
        ctype = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        return self._send(200, target.read_bytes(), ctype)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            body = {}
        parts = [p for p in u.path.strip("/").split("/") if p]

        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "media":
            return self._json(self._media_action(parts[2], body))
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "mic":
            return self._json(self._mic_action(parts[2], body))
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "vpn":
            return self._json(self._vpn_action(parts[2], body))
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "spotify":
            return self._json(self._spotify_action(parts[2], body))
        return self._json({"error": "unknown endpoint"}, 404)

    # -- actions ---------------------------------------------------

    def _media_action(self, action, body):
        player = body.get("player") or PREFERRED_PLAYER["name"]
        base = ["playerctl"] + (["-p", player] if player else [])
        if action in ("play-pause", "next", "previous", "stop"):
            run(base + [action])
        elif action == "seek":
            run(base + ["position", str(float(body.get("position", 0)))])
        elif action == "volume":
            run(base + ["volume", str(max(0.0, min(1.0, float(body.get("volume", 0.5)))))])
        elif action == "select":
            PREFERRED_PLAYER["name"] = body.get("player")
        else:
            return {"error": "unknown media action"}
        return {"ok": True}

    def _mic_action(self, action, body):
        if action == "toggle":
            run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "toggle"])
        elif action == "mute":
            run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "1"])
        elif action == "unmute":
            run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0"])
        elif action == "volume":
            run(["pactl", "set-source-volume", "@DEFAULT_SOURCE@",
                 "{}%".format(max(0, min(150, int(body.get("volume", 100)))))])
        elif action == "select":
            src = body.get("source")
            if src:
                run(["pactl", "set-default-source", src])
        else:
            return {"error": "unknown mic action"}
        return {"ok": True}

    def _redirect_uri(self):
        # Must match the URI registered in the Spotify app dashboard exactly.
        return "http://{}:{}/callback".format(HOST, PORT)

    @staticmethod
    def _page(title, msg):
        return ("<!doctype html><meta charset=utf-8>"
                "<title>{0}</title>"
                "<body style=\"background:#0a0c0f;color:#e8eef6;font-family:system-ui;"
                "display:grid;place-items:center;height:100vh;margin:0;text-align:center\">"
                "<div><h1 style=\"font-weight:500\">{0}</h1>"
                "<p style=\"color:#8b98a8\">{1}</p></div>").format(title, msg).encode()

    def _spotify_action(self, action, body):
        if action == "play":
            ok, info = spotify.play_context(body.get("uri", ""))
            return {"ok": ok, "via": info} if ok else {"ok": False, "error": info}
        if action == "client_id":
            cid = (body.get("client_id") or "").strip()
            if not cid:
                return {"error": "empty client_id"}
            spotify.set_client_id(cid)
            return {"ok": True}
        if action == "reset":
            spotify.reset()
            return {"ok": True}
        if action == "refresh":
            try:
                spotify.fetch_library(force=True)
                return {"ok": True}
            except Exception as e:
                return {"ok": False, "error": str(e)}
        return {"error": "unknown spotify action"}

    def _vpn_action(self, action, body):
        name = body.get("name")
        if not name:
            return {"error": "no profile name"}
        if action == "up":
            # May fail if the profile needs interactive credentials.
            out = run(["nmcli", "connection", "up", "id", name], timeout=45)
            return {"ok": bool(out), "output": out}
        if action == "down":
            run(["nmcli", "connection", "down", "id", name], timeout=20)
            return {"ok": True}
        return {"error": "unknown vpn action"}


def main():
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print("dashboard backend on http://{}:{}".format(HOST, PORT), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
