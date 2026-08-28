"""Intel Arc (xe driver) stats for the dashboard.

Usage comes from DRM fdinfo, not the PMU. The xe PMU needs
perf_event_paranoid <= 0 or CAP_PERFMON; fdinfo needs neither, so this
works as a normal user with no system-wide security change.

Per client fd the kernel exposes:
    drm-cycles-<engine>        busy cycles
    drm-total-cycles-<engine>  elapsed cycles (advances for everyone)
so utilisation = sum(delta busy) / delta elapsed.

Caveat: /proc/<pid>/fdinfo is only readable for our own processes, so GPU
work owned by root or another user is invisible here. Every normal client
(compositor, browsers, players) runs as the user, so in practice this
tracks the real number closely.
"""

import os
import time
from pathlib import Path

HWMON_GLOB = "/sys/class/drm/card*/device/hwmon/hwmon*"
DRI_PREFIX = "/dev/dri/"

# rcs = render/3D, ccs = compute, vcs = video decode,
# vecs = video enhance, bcs = blitter/copy
HEADLINE = ("rcs", "ccs")
VIDEO = ("vcs", "vecs")

_hwmon = {"path": None, "temps": {}}
_fds = {"pairs": [], "ts": 0.0}
_prev = {"busy": {}, "total": {}, "ts": 0.0}
_energy = {"uj": None, "ts": 0.0}

FD_RESCAN = 20.0


# ---------------------------------------------------------------- hwmon

def _find_hwmon():
    if _hwmon["path"] and Path(_hwmon["path"]).is_dir():
        return _hwmon["path"]
    import glob
    for h in glob.glob(HWMON_GLOB):
        try:
            if (Path(h) / "name").read_text().strip() in ("xe", "i915"):
                _hwmon["path"] = h
                _hwmon["temps"] = {}
                for lbl in Path(h).glob("temp*_label"):
                    try:
                        _hwmon["temps"][lbl.read_text().strip()] = str(lbl)[:-6] + "_input"
                    except Exception:
                        pass
                return h
        except Exception:
            continue
    return None


def _read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return None


def temps():
    """Returns (package_temp_c, vram_temp_c); either may be None."""
    if not _find_hwmon():
        return None, None
    out = []
    for key in ("pkg", "vram"):
        p = _hwmon["temps"].get(key)
        v = _read_int(p) if p else None
        out.append(round(v / 1000.0, 1) if v is not None else None)
    return out[0], out[1]


def power_w():
    """Average watts since the last call, from the energy counter."""
    h = _find_hwmon()
    if not h:
        return None
    uj = _read_int(os.path.join(h, "energy1_input"))
    now = time.monotonic()
    if uj is None:
        return None
    prev_uj, prev_ts = _energy["uj"], _energy["ts"]
    _energy["uj"], _energy["ts"] = uj, now
    if prev_uj is None or now <= prev_ts or uj < prev_uj:
        return None
    return round((uj - prev_uj) / 1e6 / (now - prev_ts), 1)


# ---------------------------------------------------------------- fdinfo

def _scan_fds():
    """(pid, fdinfo_path) for every fd pointing at /dev/dri/*."""
    pairs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        fddir = "/proc/{}/fd".format(pid)
        try:
            fds = os.listdir(fddir)
        except Exception:
            continue
        for fd in fds:
            try:
                if os.readlink(os.path.join(fddir, fd)).startswith(DRI_PREFIX):
                    pairs.append((pid, "/proc/{}/fdinfo/{}".format(pid, fd)))
            except Exception:
                continue
    return pairs


def _sample():
    """Sum busy cycles per engine, deduped by drm-client-id."""
    now = time.monotonic()
    if now - _fds["ts"] > FD_RESCAN or not _fds["pairs"]:
        _fds["pairs"] = _scan_fds()
        _fds["ts"] = now

    busy, total, seen = {}, {}, set()
    vram = 0
    for _, path in _fds["pairs"]:
        try:
            with open(path) as f:
                fields = {}
                for line in f:
                    k, _, v = line.partition(":")
                    fields[k.strip()] = v.strip()
        except Exception:
            continue
        if not fields.get("drm-driver", "").startswith(("xe", "i915")):
            continue
        cid = fields.get("drm-client-id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)

        for k, v in fields.items():
            if k.startswith("drm-cycles-"):
                eng = k[len("drm-cycles-"):]
                try:
                    busy[eng] = busy.get(eng, 0) + int(v)
                except ValueError:
                    pass
            elif k.startswith("drm-total-cycles-"):
                eng = k[len("drm-total-cycles-"):]
                try:
                    total[eng] = max(total.get(eng, 0), int(v))
                except ValueError:
                    pass
        rv = fields.get("drm-resident-vram0", "")
        if rv.endswith("KiB"):
            try:
                vram += int(rv.split()[0]) * 1024
            except Exception:
                pass
    return busy, total, vram, now


def usage():
    """Returns dict with render/video utilisation percentages + vram bytes."""
    busy, total, vram, now = _sample()
    prev_busy, prev_total, prev_ts = _prev["busy"], _prev["total"], _prev["ts"]
    _prev.update({"busy": busy, "total": total, "ts": now})

    def pct(engines):
        num = den = 0
        for eng in engines:
            b, t = busy.get(eng), total.get(eng)
            pb, pt = prev_busy.get(eng), prev_total.get(eng)
            if None in (b, t, pb, pt):
                continue
            db, dt = b - pb, t - pt
            if dt > 0 and db >= 0:
                num += db
                den = max(den, dt)
        if den <= 0:
            return None
        return round(min(100.0, num / den * 100.0), 1)

    if not prev_ts:
        return {"render": None, "video": None, "vram": vram}
    return {"render": pct(HEADLINE), "video": pct(VIDEO), "vram": vram}


def stats():
    pkg, vram_t = temps()
    u = usage()
    return {
        "temp": pkg,
        "vram_temp": vram_t,
        "power": power_w(),
        "usage": u["render"],
        "video": u["video"],
        "vram_used": u["vram"],
    }
