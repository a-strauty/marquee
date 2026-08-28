"""Spotify integration for the touch dashboard.

Split by capability, because the two halves have very different costs:

  * LISTING  playlists / albums / liked  -> Spotify Web API (OAuth PKCE).
             Spotify has no anonymous access, so this needs a one-time
             app registration. PKCE means a Client ID only - no secret.

  * PLAYING  a playlist -> MPRIS DBus LoadContextUri on the local client.
             Instant, no network round trip, and no Premium-gated
             /me/player Web API call. Spotify's Linux client advertises
             SupportedUriSchemes = ["spotify"].

Spotify's MPRIS has no Playlists interface (HasTrackList is false), which
is why listing can't also come from DBus.

Tokens live in ~/.config/dashboard/spotify.json, mode 0600.
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CONF_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "dashboard"
CONF_FILE = CONF_DIR / "spotify.json"

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"

SCOPES = " ".join([
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
])

MPRIS_DEST = "org.mpris.MediaPlayer2.spotify"
MPRIS_PATH = "/org/mpris/MediaPlayer2"

_LOCK = threading.Lock()
_PENDING = {}          # state -> code_verifier
_TOKEN = {"access": None, "expires": 0}
_CACHE = {"data": None, "ts": 0}
_ME = {"id": None}
CACHE_TTL = 600.0


# ---------------------------------------------------------------- config

def _load():
    try:
        return json.loads(CONF_FILE.read_text())
    except Exception:
        return {}


def _save(conf):
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    CONF_FILE.write_text(json.dumps(conf, indent=2))
    try:
        CONF_FILE.chmod(0o600)
    except Exception:
        pass


def client_id():
    return os.environ.get("DASH_SPOTIFY_CLIENT_ID") or _load().get("client_id") or ""


def set_client_id(cid):
    conf = _load()
    conf["client_id"] = cid.strip()
    _save(conf)


def _refresh_token():
    return _load().get("refresh_token") or ""


def status():
    return {
        "configured": bool(client_id()),
        "authorized": bool(_refresh_token()),
        "config_file": str(CONF_FILE),
    }


def reset():
    conf = _load()
    conf.pop("refresh_token", None)
    _save(conf)
    with _LOCK:
        _TOKEN.update({"access": None, "expires": 0})
        _CACHE.update({"data": None, "ts": 0})


# ---------------------------------------------------------------- oauth (PKCE)

def _b64url(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def begin_auth(redirect_uri):
    cid = client_id()
    if not cid:
        raise RuntimeError("no client_id configured")
    verifier = _b64url(secrets.token_bytes(64))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(16))
    with _LOCK:
        _PENDING[state] = verifier
        # keep the pending map from growing without bound
        for k in list(_PENDING)[:-8]:
            _PENDING.pop(k, None)
    q = urllib.parse.urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "state": state,
    })
    return "{}?{}".format(AUTH_URL, q)


def _post_token(fields):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def finish_auth(code, state, redirect_uri):
    with _LOCK:
        verifier = _PENDING.pop(state, None)
    if not verifier:
        raise RuntimeError("unknown or expired auth state")
    tok = _post_token({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id(),
        "code_verifier": verifier,
    })
    if "refresh_token" not in tok:
        raise RuntimeError("no refresh_token in response")
    conf = _load()
    conf["refresh_token"] = tok["refresh_token"]
    _save(conf)
    with _LOCK:
        _TOKEN["access"] = tok.get("access_token")
        _TOKEN["expires"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        _CACHE.update({"data": None, "ts": 0})
    return True


def access_token():
    with _LOCK:
        if _TOKEN["access"] and time.time() < _TOKEN["expires"]:
            return _TOKEN["access"]
    rt = _refresh_token()
    if not rt:
        return None
    tok = _post_token({
        "grant_type": "refresh_token",
        "refresh_token": rt,
        "client_id": client_id(),
    })
    with _LOCK:
        _TOKEN["access"] = tok.get("access_token")
        _TOKEN["expires"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    # Spotify may hand back a rotated refresh token.
    if tok.get("refresh_token"):
        conf = _load()
        conf["refresh_token"] = tok["refresh_token"]
        _save(conf)
    return _TOKEN["access"]


# ---------------------------------------------------------------- web api

def _api_get(path, params=None):
    tok = access_token()
    if not tok:
        raise RuntimeError("not authorized")
    url = "{}{}".format(API, path)
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + tok})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def _smallest_image(images, floor=200):
    """Pick the smallest cover at or above `floor` px, else the largest."""
    if not images:
        return ""
    sized = [i for i in images if i.get("width")]
    if not sized:
        return images[0].get("url", "")
    ok = [i for i in sized if i["width"] >= floor]
    pick = min(ok, key=lambda i: i["width"]) if ok else max(sized, key=lambda i: i["width"])
    return pick.get("url", "")


def _paged(path, params, cap=200):
    out, offset = [], 0
    while len(out) < cap:
        p = dict(params or {})
        p.update({"limit": 50, "offset": offset})
        data = _api_get(path, p)
        items = data.get("items") or []
        out.extend(items)
        if not data.get("next") or not items:
            break
        offset += len(items)
    return out[:cap]


def me_id():
    """Spotify user id, cached. Needed to build the Liked Songs URI."""
    if _ME["id"]:
        return _ME["id"]
    try:
        _ME["id"] = _api_get("/me").get("id") or ""
    except Exception:
        _ME["id"] = ""
    return _ME["id"]


def fetch_library(force=False):
    with _LOCK:
        if not force and _CACHE["data"] and time.time() - _CACHE["ts"] < CACHE_TTL:
            return _CACHE["data"]

    playlists = []
    for p in _paged("/me/playlists", {}):
        if not p:
            continue
        playlists.append({
            "uri": p.get("uri"),
            "name": p.get("name") or "Untitled",
            "art": _smallest_image(p.get("images") or []),
            "count": (p.get("tracks") or {}).get("total", 0),
            "owner": ((p.get("owner") or {}).get("display_name") or ""),
        })

    albums = []
    for a in _paged("/me/albums", {}):
        al = (a or {}).get("album") or {}
        albums.append({
            "uri": al.get("uri"),
            "name": al.get("name") or "Untitled",
            "art": _smallest_image(al.get("images") or []),
            "count": al.get("total_tracks", 0),
            "owner": ", ".join(ar.get("name", "") for ar in (al.get("artists") or [])[:2]),
        })

    liked_total = 0
    try:
        liked_total = _api_get("/me/tracks", {"limit": 1}).get("total", 0)
    except Exception:
        pass

    data = {
        "playlists": playlists,
        "albums": albums,
        # spotify:collection:tracks and spotify:collection are both silently
        # ignored by the client; only the user-scoped form actually plays.
        "liked": {"uri": ("spotify:user:{}:collection".format(me_id()) if me_id() else ""),
                  "name": "Liked Songs", "count": liked_total, "art": "", "owner": "",
                  "kind": "liked"},
        "fetched": time.time(),
    }
    with _LOCK:
        _CACHE.update({"data": data, "ts": time.time()})
    return data


# ---------------------------------------------------------------- playback

def play_context(uri):
    """Play a Spotify context on the local client via MPRIS.

    Order matters, and not the way you would guess. Spotify's own
    LoadContextUri accepts the call and returns success -- then does
    absolutely nothing. The generic MPRIS OpenUri is what actually
    starts playback. Verified on com.spotify.Client 1.2.95.

    So OpenUri goes first, and LoadContextUri is kept only as a fallback
    in case a future client build reverses this.
    """
    if not uri or not uri.startswith("spotify:"):
        return False, "bad uri"

    before = _trackid()
    err = ""
    for method in ("OpenUri", "LoadContextUri"):
        p = subprocess.run(
            ["gdbus", "call", "--session", "--dest", MPRIS_DEST,
             "--object-path", MPRIS_PATH,
             "--method", "org.mpris.MediaPlayer2.Player." + method, uri],
            capture_output=True, text=True, timeout=8,
        )
        if p.returncode != 0:
            err = (p.stderr or "").strip()[:200]
            continue
        # gdbus exits 0 even when the method is a silent no-op, so confirm
        # against the actual track before claiming success.
        for _ in range(6):
            time.sleep(0.25)
            if _trackid() != before:
                return True, method
    return False, err or "call accepted but playback did not change"


def _trackid():
    try:
        p = subprocess.run(
            ["playerctl", "-p", "spotify", "metadata", "--format", "{{mpris:trackid}}"],
            capture_output=True, text=True, timeout=3,
        )
        return p.stdout.strip() if p.returncode == 0 else ""
    except Exception:
        return ""
