#!/usr/bin/env bash
# Launch the touch-panel dashboard: backend + kiosk browser.
#
# The browser is started with a dedicated profile and a custom Wayland app_id
# ("dashkiosk") so Hyprland window rules can target it without touching your
# normal browsing session.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Machine-specific settings (gitignored). Everything has a fallback, so a
# missing config.env degrades rather than breaks. Existing environment
# wins, so you can still override per-invocation.
if [[ -f "$HERE/config.env" ]]; then
    set -a; . "$HERE/config.env"; set +a
fi

PORT="${DASH_PORT:-8770}"
URL="http://127.0.0.1:${PORT}/"
APPID="dashkiosk"
PROFILE="${XDG_DATA_HOME:-$HOME/.local/share}/dashboard-kiosk"

# --- backend -----------------------------------------------------------
if ! curl -sf --max-time 2 "http://127.0.0.1:${PORT}/api/state" >/dev/null 2>&1; then
  echo ":: starting backend on ${PORT}"
  DASH_PORT="$PORT" setsid python3 "${HERE}/server.py" >"${HERE}/.server.log" 2>&1 &
  for _ in $(seq 1 25); do
    curl -sf --max-time 1 "http://127.0.0.1:${PORT}/api/state" >/dev/null 2>&1 && break
    sleep 0.3
  done
fi
echo ":: backend up"

# --- browser -----------------------------------------------------------
if command -v chromium >/dev/null 2>&1; then
  echo ":: launching chromium kiosk"
  exec chromium \
    --class="$APPID" \
    --user-data-dir="${PROFILE}-chromium" \
    --kiosk --app="$URL" \
    --ozone-platform=wayland \
    --start-fullscreen \
    --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
    --disable-features=TranslateUI,PinchZoom \
    --overscroll-history-navigation=0 \
    --autoplay-policy=no-user-gesture-required \
    --check-for-update-interval=31536000
fi

# Firefox / Zen fallback. MOZ_APP_REMOTINGNAME sets the Wayland app_id.
BROWSER="$(command -v firefox || command -v zen-browser)"
echo ":: launching ${BROWSER##*/} kiosk"

mkdir -p "$PROFILE"
cat > "${PROFILE}/user.js" <<'PREFS'
user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("browser.sessionstore.resume_from_crash", false);
user_pref("datareporting.policy.dataSubmissionEnabled", false);
user_pref("datareporting.healthreport.uploadEnabled", false);
user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("dom.w3c_touch_events.enabled", 1);
user_pref("apz.allow_zooming", false);
user_pref("apz.allow_double_tap_zooming", false);
user_pref("browser.tabs.inTitlebar", 0);
user_pref("media.autoplay.default", 0);
PREFS

export MOZ_APP_REMOTINGNAME="$APPID"
export MOZ_ENABLE_WAYLAND=1
exec "$BROWSER" --kiosk --new-instance --profile "$PROFILE" "$URL"
