# Touch Panel Dashboard

Kiosk dashboard for the **Wisecoco 3840x1100 touchscreen** on `HDMI-A-3`.
Media control, system stats, clock/weather, VPN state, and a big mic-mute button.

```
dashboard/
├── server.py           # stdlib-only backend + JSON API (loopback:8770)
├── static/index.html   # the dashboard UI, laid out for 3840x1100
├── spotify.py          # Spotify Web API (PKCE) + MPRIS playback
├── gpu.py              # Intel Arc (xe) temps, power, utilisation
├── run-dashboard.sh    # starts backend + kiosk browser
├── config.env.example  # machine-specific settings; copy to config.env
├── dashboard.lua       # Hyprland Lua config, outside the ML4W tree
└── install.sh          # idempotent ML4W installer (--check to dry-run)
```

## What's on it

| Column | Contents |
|---|---|
| 1 | Clock, date, **scrollable Spotify library**, weather |
| 2 | Now playing — art, title/artist/album, seek bar, transport, volume, player switcher |
| 3 | CPU / GPU / RAM / disk bars; CPU, GPU, NVMe temps; GPU power, VRAM, video engine, network |
| 4 | Giant mic mute button, VPN status + connect toggle |

**Touch gestures**
- Tap the mic panel → toggle mute
- **Long-press** the mic panel (0.65s) → cycle input device (Yeti ↔ built-in)
- Tap the seek bar → scrub; tap the volume bar → set volume
- Tap a player chip → switch between Spotify / Zen
- Tap a library row → play that playlist/album; tabs switch Playlists ↔ Albums

## Backend API

`GET /api/state` returns everything. Sampler thread refreshes at 1 Hz
(sensors 5s, disks 30s, weather 15min).

```
POST /api/media/{play-pause,next,previous,seek,volume,select}
POST /api/mic/{toggle,mute,unmute,volume,select}
POST /api/vpn/{up,down}
GET  /api/spotify/status            # {configured, authorized}
GET  /api/spotify/login             # 302 -> Spotify consent (visit once)
GET  /api/spotify/library[?force]   # playlists + albums + liked
POST /api/spotify/play              # {"uri": "spotify:playlist:..."} via DBus
POST /api/spotify/client_id         # {"client_id": "..."}
POST /api/spotify/{reset,refresh}
GET  /api/art?u=<mpris artUrl>     # proxies file:// and https:// album art
```

Mic control targets `@DEFAULT_SOURCE@`, so it follows hotplug automatically —
unplug the Yeti and it falls back to built-in without restarting anything.

## Porting to another machine

Everything machine-specific lives in **`config.env`**, which is gitignored.
The repo itself contains no absolute paths, hostnames, or secrets.

```bash
git clone <repo> dashboard && cd dashboard
cp config.env.example config.env
./install.sh --detect      # prints this machine's monitors, touch devices,
                           # interfaces, filesystems and waybar themes
$EDITOR config.env
./install.sh
```

`--detect` reads the real values off the host so you can paste them in.
Every setting has a fallback, so a missing `config.env` degrades rather
than breaks — and a blank `DASH_TOUCH_DEVICE` simply skips the
touch-to-output mapping, which is what you want on a non-touch panel.

### What is and isn't portable

| | Status |
|---|---|
| Paths | `dashboard.lua` derives its own directory via `debug.getinfo`; both shell scripts use `$HERE`. Clone anywhere. |
| Monitor, touch device, workspace | `config.env` |
| Waybar outputs + theme names | `config.env` |
| Network interface, filesystems, weather, port | `config.env` |
| Spotify credentials | **never in the repo** — `~/.config/dashboard/spotify.json`, mode 0600. Each machine authorises once. |
| Hyprland hook | `install.sh` writes the absolute `dofile()` line into `~/.config/hypr/custom.lua`, outside the repo. |

### Hard requirements

These are assumed rather than configurable:

- **Hyprland running the Lua config** (`~/.config/hypr/hyprland.lua`).
  `install.sh` aborts if it's missing. A `.conf`-based Hyprland needs
  `dashboard.lua` rewritten in hyprlang.
- **ML4W-style waybar layout** (`themes/<name>/config` + `waybar-theme.sh`).
  On plain waybar, set the bar's `output` by hand and ignore step 3.
- **PipeWire** (`pactl`), **playerctl**, **NetworkManager** (`nmcli`) for the
  mic, media and VPN panels. Each degrades to `—` independently if absent.
- **Intel GPU** (`xe`/`i915`) for the GPU stats — see below. On AMD/NVIDIA
  the tiles read `—`; nothing crashes, but `gpu.py` needs a new backend.

### Layout is tuned for 3840x1100

The four-column grid is sized for that bar panel. A different aspect ratio
needs `.grid`'s `grid-template-columns` retuned in `static/index.html`; the
panels themselves are flexbox and will reflow.

## Spotify library — one-time setup

The library list needs the **Spotify Web API**. Spotify has no anonymous
access, so this requires registering an app. It uses the **PKCE** flow, so
only a Client ID is needed — there is no secret to store or leak.

1. Go to <https://developer.spotify.com/dashboard> → **Create app**
   (any name/description).
2. Add this **exact** Redirect URI and save:

   ```
   http://127.0.0.1:8770/callback
   ```

   Loopback `http://` URIs are the one exception to Spotify's HTTPS rule.
3. Copy the **Client ID** and register it:

   ```bash
   curl -X POST localhost:8770/api/spotify/client_id \
        -H 'Content-Type: application/json' \
        -d '{"client_id":"PASTE_IT_HERE"}'
   ```

   (Or set `DASH_SPOTIFY_CLIENT_ID`, or write it into
   `~/.config/dashboard/spotify.json`.)
4. Authorize once — open this in a normal browser on this machine:

   ```
   http://127.0.0.1:8770/api/spotify/login
   ```

The refresh token is saved to `~/.config/dashboard/spotify.json` (mode 0600)
and renews itself; you should not have to repeat this. `POST /api/spotify/reset`
revokes it locally.

### How listing and playing differ

| | Mechanism | Notes |
|---|---|---|
| **List** playlists/albums | Web API `/me/playlists`, `/me/albums` | needs OAuth; cached 10 min |
| **Play** a playlist | MPRIS DBus `LoadContextUri` | local, instant, no Premium-gated `/me/player` call |

Spotify's MPRIS has **no `Playlists` interface** (`HasTrackList: false`), so
listing cannot come from DBus. But it advertises
`SupportedUriSchemes: ["spotify"]`, so playback needs no Web API call at all.
Spotify here is a Flatpak (`com.spotify.Client` 1.2.95); it still publishes
MPRIS on the session bus normally.

### Two client quirks worth knowing

**`LoadContextUri` is a liar.** Spotify's own method accepts the call, returns
success, and does nothing. The generic MPRIS `OpenUri` is what actually plays.
`gdbus` exits 0 either way, so exit status cannot be trusted — `play_context()`
tries `OpenUri` first and then **verifies against `mpris:trackid`** before
reporting success. Failures surface as a red flash on the row.

**Liked Songs needs the user-scoped URI.** Both `spotify:collection:tracks`
and `spotify:collection` are silently ignored. Only
`spotify:user:<your-id>:collection` works, so the backend fetches `/me` once
and builds it. This is why a no-op looks identical to success without the
trackid check.

## Config knobs (env vars)

| Var | Default | Meaning |
|---|---|---|
| `DASH_PORT` | `8770` | listen port |
| `DASH_IFACE` | `eno2` | interface for throughput |
| `DASH_DISKS` | `/,/mnt/storage,/mnt/share` | disks to show |
| `DASH_WEATHER` | `1` | `0` disables the outbound wttr.in call |
| `DASH_WEATHER_LOC` | *(blank)* | blank = geolocate by IP; else e.g. `Sacramento` |
| `DASH_UNITS` | `F` | `F` or `C` |
| `DASH_SPOTIFY_CLIENT_ID` | *(unset)* | overrides the stored Client ID |

> Weather makes an outbound HTTPS request to `wttr.in`. Blank location means
> wttr.in geolocates from your public IP. Set `DASH_WEATHER=0` to turn it off.

## GPU stats (Intel Arc B580, `xe` driver)

Utilisation comes from **DRM fdinfo**, not the PMU. The `xe` PMU needs
`perf_event_paranoid <= 0` or `CAP_PERFMON`; fdinfo needs neither, so this
runs as a normal user with **no system-wide security change**.

Per client fd the kernel exposes `drm-cycles-<engine>` (busy) and
`drm-total-cycles-<engine>` (elapsed), so utilisation is
`Σ Δbusy / Δelapsed`, deduped by `drm-client-id`. Engines: `rcs` render,
`ccs` compute, `vcs`/`vecs` video, `bcs` blit. The headline GPU figure is
render+compute; the video engines get their own tile (it lights up when
you're watching something).

Temps and power come from the `xe` hwmon node:

| Node | Meaning |
|---|---|
| `temp2_input` (`pkg`) | GPU package — the headline temp |
| `temp3_input` (`vram`) | VRAM (there are also 12 per-channel sensors) |
| `energy1_input` | µJ counter; differentiated into watts |

Two caveats:

- **fdinfo is only readable for your own processes**, so GPU work owned by
  root or another user is invisible. Every normal client (compositor,
  browsers, players) runs as you, so it tracks closely in practice.
- **`xe` exposes no total-VRAM node**, so VRAM is shown as an absolute
  figure rather than a percentage bar. `drm-resident-vram0` is summed
  across clients.

Costs one `/proc` walk every 2s; the fd list is cached and rescanned every
20s rather than rebuilt each tick.

## ML4W integration (update-safe)

> **ML4W 2.10.1 runs Hyprland's Lua config**, not the `.conf` tree.
> `~/.config/hypr/hyprland.lua` is the entrypoint. Every `.conf` file under
> `~/.config/hypr/` is dead legacy — **editing them silently does nothing.**
> Hyprland even says so if you try `hyprctl keyword`:
> *"keyword can't work with non-legacy parsers."*

`~/.config/{hypr,waybar,ml4w}` are all symlinks into
`~/.mydotfiles/com.ml4w.dotfiles/`, so everything we touch lands in the tree
the updater manages.

**Run `./install.sh` to apply, and re-run it after any ML4W update.**
`./install.sh --check` dry-runs it; it is fully idempotent.

| # | Target | Why it survives |
|---|---|---|
| 1 | *(nothing)* | `hyprland.lua` already does `require("monitors")`, and nwg-displays' `monitors.lua` defines all three monitors. Nothing to do. |
| 2 | `~/.config/hypr/custom.lua` — one `dofile` line | loaded by `require("custom")`; if an update replaces it, re-run `install.sh` |
| 3 | `themes/adam-minimal/` + `ml4w/settings/waybar-theme.sh` | custom theme is unshipped; `.config/ml4w/settings` **is** in the `restore` list |

All real config lives in `dashboard.lua` **outside the dotfiles tree**, pulled
in by that single `dofile`. The updater cannot reach it. Delete the line to
remove the dashboard.

### The Lua API

Reference stub: **`/usr/share/hypr/stubs/hl.meta.lua`** (1777 lines) — the
authoritative field list.

```lua
hl.device({ name = "istaric-istaric", output = "HDMI-A-3" })
hl.workspace_rule({ workspace = "name:panel", monitor = "HDMI-A-3", ... })
hl.window_rule({ name = "...", match = { class = "dashkiosk" }, ... })
hl.on("hyprland.start", function() hl.exec_cmd("...") end)
```

Gotchas found the hard way:

- **`hyprctl configerrors` always returns empty here** — even for a deliberate
  bogus keyword in a known-loaded file. Lua config errors surface as an
  **on-screen banner**, not through that command. Don't trust it.
- `no_border` / `no_rounding` are `workspace_rule` fields and are **rejected**
  by `hl.window_rule`. The workspace rule covers the whole panel anyway.
- Dispatchers are namespaced tables, not strings:
  `hyprctl dispatch 'hl.dsp.window.move({ workspace = "2", window = "class:spotify" })'`
- Named workspaces get negative IDs (`panel` is `-1337`); numeric IDs 1-10 are
  already claimed, which is why the panel uses a name.

### Why not edit the shipped files

- **`themes/ml4w-minimal/config`** — `~/.config/waybar` is not in the restore
  list, so the edit dies on the next update. Worse, `launch.sh` runs `sed -i`
  on the *active* theme's config at every start (`_toggle_module`).
- **`conf/windowrules/*.lua`** — forking upstream's rules to add three kiosk
  rules would permanently cost you their updates.

### One conflict this surfaced

`~/.config/hypr/custom.lua` contains:

```lua
for i = 3, 4 do
    hl.workspace_rule({ ..., default = i == 6, persistent = true })
end
```

`i` never equals 6 in a `3, 4` loop, so **no workspace on DP-1 is ever the
default**. Pre-existing, unrelated to the dashboard, and left alone — but
probably not what was intended.

## Open decision: idle / lock

`hypridle` dims at 8 min and locks at 10 min across **all** outputs, so the
panel goes dark with everything else. Options:

- **Leave as-is** — panel sleeps with the desk. Safest.
- **Inhibit while playing** — add `windowrule = idleinhibit fullscreen, class:^(dashkiosk)$`
  to `dashboard.lua` (as `idle_inhibit`); keeps the panel awake but suppresses lock machine-wide.
- **Panel-only DPMS** — a hypridle listener that blanks DP-1/DP-2 only,
  leaving HDMI-A-3 lit. Most work, best result. `hypridle.conf` **is** in the
  restore list, so editing it is safe. Note hypridle still uses the classic
  syntax and calls `hyprctl dispatch`, which now needs the Lua form.

I did not pick one for you.

## Running it

```bash
./install.sh --check     # show what would change, write nothing
./install.sh             # apply + reload hyprland/waybar

# run the kiosk by hand without installing anything
./run-dashboard.sh

# backend only, to poke the API
python3 server.py
curl -s localhost:8770/api/state | jq
```

Chromium is **not installed** and gives a noticeably better kiosk (true
`--app` mode, PinchZoom disable, no session-restore bubbles). The launcher
prefers it and falls back to Firefox/Zen with a dedicated profile:

```bash
sudo pacman -S chromium
```
