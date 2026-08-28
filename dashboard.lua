-- ============================================================
--  Touch dashboard panel — Hyprland (Lua config)
--
--  ML4W 2.10.1 runs Hyprland's *Lua* config (~/.config/hypr/hyprland.lua).
--  The .conf tree is dead legacy and is not parsed at all.
--
--  This file lives OUTSIDE the ML4W dotfiles tree, so no update can
--  touch it. It is pulled in by a single dofile() line appended to
--  ~/.config/hypr/custom.lua (which hyprland.lua loads via
--  require("custom")). Remove that line to remove the dashboard.
--
--  Machine-specific values come from config.env next to this file, so
--  nothing here needs editing per machine. See config.env.example.
--
--  API reference: /usr/share/hypr/stubs/hl.meta.lua
-- ============================================================

-- Hyprland dofile()s this by absolute path, so derive our own directory
-- rather than hardcoding one.
local function script_dir()
    local src = debug.getinfo(1, "S").source
    if src:sub(1, 1) == "@" then src = src:sub(2) end
    return src:match("(.*)/") or "."
end

-- Minimal KEY="value" parser: the same config.env the shell scripts source.
local function load_env(path)
    local cfg, f = {}, io.open(path, "r")
    if not f then return cfg end
    for line in f:lines() do
        if not line:match("^%s*#") then
            local k, v = line:match("^%s*([%w_]+)%s*=%s*(.-)%s*$")
            if k then
                v = v:gsub('^"(.*)"$', "%1"):gsub("^'(.*)'$", "%1")
                cfg[k] = v
            end
        end
    end
    f:close()
    return cfg
end

local DIR = script_dir()
local cfg = load_env(DIR .. "/config.env")

local MONITOR   = cfg.DASH_MONITOR or "HDMI-A-3"
local WORKSPACE = cfg.DASH_WORKSPACE or "panel"
local TOUCH     = cfg.DASH_TOUCH_DEVICE or ""

-- --- 1. Map the touchscreen to the panel --------------------
-- Without this the touch surface is stretched across the whole
-- monitor layout, so taps land on the wrong screen. Skipped when no
-- device is configured, so a non-touch panel still works.
if TOUCH ~= "" then
    hl.device({
        name   = TOUCH,
        output = MONITOR,
    })
end

-- --- 2. Dedicated kiosk workspace ---------------------------
-- Named rather than numbered so it can't collide with the numeric
-- workspaces defined in custom.lua.
hl.workspace_rule({
    workspace   = "name:" .. WORKSPACE,
    monitor     = MONITOR,
    default     = true,
    persistent  = true,
    gaps_in     = 0,
    gaps_out    = 0,
    no_border   = true,
    no_rounding = true,
    no_shadow   = true,
    decorate    = false,
})

-- --- 3. Kiosk window rules ----------------------------------
-- The launcher gives the browser the Wayland app_id "dashkiosk",
-- so this never touches normal browser windows.
hl.window_rule({
    name             = "dashboard-kiosk",
    match            = { class = "dashkiosk" },
    workspace        = "name:" .. WORKSPACE,
    fullscreen       = true,
    no_anim          = true,
    no_initial_focus = true,
    -- NB: no_border / no_rounding are workspace_rule fields, not
    -- window_rule fields. The panel workspace rule above already
    -- strips borders, rounding and shadows for everything on it.
})

-- --- 4. Autostart -------------------------------------------
hl.on("hyprland.start", function()
    hl.exec_cmd(DIR .. "/run-dashboard.sh")
end)
