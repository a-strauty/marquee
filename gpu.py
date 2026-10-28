"""GPU stats for the dashboard: Intel (xe / i915) and AMD (amdgpu).

The driver is detected from the card's hwmon node, so nothing needs
configuring.

Intel: usage comes from DRM fdinfo, not the PMU. The xe PMU needs
perf_event_paranoid <= 0 or CAP_PERFMON; fdinfo needs neither, so this
works as a normal user with no system-wide security change.

Per client fd the kernel exposes:
    drm-cycles-<engine>        busy cycles
    drm-total-cycles-<engine>  elapsed cycles (advances for everyone)
so utilisation = sum(delta busy) / delta elapsed.

AMD: overall usage and VRAM come straight from the card's sysfs
(gpu_busy_percent, mem_info_vram_used), which is system-wide. The video
engines have no sysfs counter, so they still come from fdinfo, where
amdgpu exposes:
    drm-engine-<engine>        busy nanoseconds
so utilisation = delta busy ns / delta wall-clock ns, per client, so a
client appearing or exiting between samples can't spike or dip the
figure.

Caveat: /proc/<pid>/fdinfo is only readable for our own processes, so GPU
work owned by root or another user is invisible to the fdinfo figures.
Every normal client (compositor, browsers, players) runs as the user, so
in practice this tracks the real number closely.
"""

import os
import time
from pathlib import Path

HWMON_GLOB = "/sys/class/drm/card*/device/hwmon/hwmon*"
DRI_PREFIX = "/dev/dri/"

INTEL = ("xe", "i915")
AMD = ("amdgpu",)

# hwmon temp labels per driver: (headline, vram)
TEMP_LABELS = {
    "xe": ("pkg", "vram"),
    "i915": ("pkg", "vram"),
    "amdgpu": ("edge", "mem"),
}

# Intel engines: rcs = render/3D, ccs = compute, vcs = video decode,
# vecs = video enhance, bcs = blitter/copy
HEADLINE = ("rcs", "ccs")
VIDEO = ("vcs", "vecs")
# amdgpu video engines. VCN 4+ decodes on the unified "enc" ring, so a
# video player's decode shows up under enc, not dec.
AMD_VIDEO = ("dec", "enc", "enc_1", "jpeg")

_hwmon = {"path": None, "driver": None, "temps": {}}
_fds = {"pairs": [], "ts": 0.0}
_prev = {"busy": {}, "total": {}, "ts": 0.0}
_energy = {"uj": None, "ts": 0.0}

FD_RESCAN = 20.0


# ---------------------------------------------------------------- hwmon

def _find_hwmon():
    if _hwmon["path"] and Path(_hwmon["path"]).is_dir():
        return _hwmon["path"]
    import glob
    for h in sorted(glob.glob(HWMON_GLOB)):
        try:
            name = (Path(h) / "name").read_text().strip()
            if name in INTEL + AMD:
                _hwmon["path"] = h
                _hwmon["driver"] = name
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


def _is_amd():
    return bool(_find_hwmon()) and _hwmon["driver"] in AMD


def _device():
    """The card's PCI device dir (hwmon lives at <device>/hwmon/hwmonN)."""
    return Path(_hwmon["path"]).parent.parent


def _read_int(path):
    try:
        return int(Path(path).read_text().strip())
    except Exception:
        return None


def temps():
    """Returns (headline_temp_c, vram_temp_c); either may be None.

    Headline is the package temp on Intel and the edge temp on AMD.
    """
    if not _find_hwmon():
        return None, None
    out = []
    for key in TEMP_LABELS[_hwmon["driver"]]:
        p = _hwmon["temps"].get(key)
        v = _read_int(p) if p else None
        out.append(round(v / 1000.0, 1) if v is not None else None)
    return out[0], out[1]


def power_w():
    """Board power in watts.

    Intel: average since the last call, from the energy counter.
    AMD: the driver's own short-window average, in microwatts.
    """
    h = _find_hwmon()
    if not h:
        return None
    if _hwmon["driver"] in AMD:
        for node in ("power1_average", "power1_input"):
            uw = _read_int(os.path.join(h, node))
            if uw is not None:
                return round(uw / 1e6, 1)
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


def _clients(drivers):
    """Yield (client_id, fields, lines) once per DRM client of `drivers`.

    `lines` keeps repeated keys, which `fields` would collapse: amdgpu
    prints one drm-engine-<name> line per hardware IP, and some IPs share
    a name (UVD and VCN decode are both "dec").
    """
    now = time.monotonic()
    if now - _fds["ts"] > FD_RESCAN or not _fds["pairs"]:
        _fds["pairs"] = _scan_fds()
        _fds["ts"] = now

    seen = set()
    for _, path in _fds["pairs"]:
        try:
            with open(path) as f:
                lines = []
                for line in f:
                    k, _, v = line.partition(":")
                    lines.append((k.strip(), v.strip()))
        except Exception:
            continue
        fields = dict(lines)
        if not fields.get("drm-driver", "").startswith(drivers):
            continue
        cid = fields.get("drm-client-id")
        if cid is None or cid in seen:
            continue
        seen.add(cid)
        yield cid, fields, lines


def _sample():
    """Intel: sum busy cycles per engine, deduped by drm-client-id."""
    now = time.monotonic()
    busy, total = {}, {}
    vram = 0
    for _, fields, _ in _clients(INTEL):
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


def _sample_amd():
    """AMD: busy ns per (client, engine)."""
    now = time.monotonic()
    busy = {}
    for cid, _, lines in _clients(AMD):
        for k, v in lines:
            if k.startswith("drm-engine-") and v.endswith(" ns"):
                key = (cid, k[len("drm-engine-"):])
                try:
                    busy[key] = busy.get(key, 0) + int(v.split()[0])
                except ValueError:
                    pass
    return busy, now


def _usage_amd():
    """Overall busy % and VRAM from sysfs; video engines from fdinfo."""
    dev = _device()
    busy, now = _sample_amd()
    prev_busy, prev_ts = _prev["busy"], _prev["ts"]
    _prev.update({"busy": busy, "total": {}, "ts": now})

    video = None
    if prev_ts and now > prev_ts:
        # Only clients present in both samples. amdgpu prints an engine
        # line only once it has been used, so no lines at all means idle.
        wall_ns = (now - prev_ts) * 1e9
        per_eng = {}
        for (cid, eng), ns in busy.items():
            pns = prev_busy.get((cid, eng))
            if eng in AMD_VIDEO and pns is not None and ns >= pns:
                per_eng[eng] = per_eng.get(eng, 0) + (ns - pns)
        # Busiest single video engine, not the sum: dec/enc/jpeg are
        # separate hardware blocks.
        video = round(min(100.0, max(per_eng.values(), default=0) / wall_ns * 100.0), 1)

    return {
        "render": _read_int(dev / "gpu_busy_percent"),
        "video": video,
        "vram": _read_int(dev / "mem_info_vram_used") or 0,
    }


def usage():
    """Returns dict with render/video utilisation percentages + vram bytes."""
    if _is_amd():
        return _usage_amd()

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
