#!/bin/sh
# Run each native keyboard test on a private display, bus and input-method profile.
set -eu
if [ "${1:-}" != --session ]; then
  profile=$(mktemp -d)
  export XDG_CONFIG_HOME="$profile/config" XDG_CACHE_HOME="$profile/cache" XDG_DATA_HOME="$profile/data"
  export GSETTINGS_BACKEND=keyfile GTK_IM_MODULE=ibus XMODIFIERS=@im=ibus
  mkdir -p "$XDG_CONFIG_HOME" "$XDG_CACHE_HOME" "$XDG_DATA_HOME"
  exec dbus-run-session -- xvfb-run -a -s '-screen 0 1600x1100x24' sh "$0" --session "$@"
fi
shift
gsettings set org.freedesktop.ibus.engine.hangul initial-input-mode hangul
ibus-daemon --daemonize --xim --panel disable
trap 'ibus exit >/dev/null 2>&1 || true' EXIT
for attempt in $(seq 1 50); do
  if ibus engine xkb:us::eng 2>/dev/null; then break; fi
  sleep 0.2
done
export OFFICE_TEST_IBUS=1
node "$(dirname "$0")/verify-handoff.mjs" "$@"
