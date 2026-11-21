#!/usr/bin/env bash
set -euo pipefail
# The install/remove probes run in a disposable CI guest.
test "${GITHUB_ACTIONS:-}" = true
sudo apt-get install -y --no-install-recommends ./electron-installer/*.deb ./rust-desktop/target/release/bundle/deb/*.deb
electron=$(realpath /usr/bin/ai-agent-office-desktop)
rust=$(realpath /usr/bin/ai-agent-office-rust)
dpkg-query -W ai-agent-office-rust ai-agent-office-desktop
test -f /usr/share/applications/ai-agent-office-desktop.desktop
test -f /usr/share/applications/ai-agent-office-rust.desktop
out="$PWD/comparison-evidence"
# Isolate the interactive test process from the runner service and use its own HOME.
sudo useradd --create-home --user-group --shell /bin/bash office-rust-ci
tools=$(mktemp -d /tmp/office-rust-check.XXXXXXXX)
sudo install -d -o office-rust-ci -g office-rust-ci "$tools/evidence"
chmod 0755 "$tools"
cp -r desktop "$tools/"
install -m0755 "$(command -v node)" "$tools/node"
install -m0755 "$(command -v tauri-driver)" "$tools/tauri-driver"
collect() { mkdir -p "$out"; sudo cp -a "$tools/evidence/." "$out"; sudo chown -R "$(id -u):$(id -g)" "$out"; }
trap collect EXIT
verify() {
  sudo --login --user office-rust-ci /usr/bin/env OFFICE_COMPARISON_PYTHON="$(command -v python)" OFFICE_HANDOFF_RECEIPT="$tools/evidence/handoff/journal-receipt.json" TAURI_DRIVER="$tools/tauri-driver" \
    TAURI_NATIVE_DRIVER="$(command -v WebKitWebDriver)" xvfb-run -a dbus-run-session -- \
    "$tools/node" "$tools/desktop/comparison/$1" "${@:2}"
}
verify verify-connection.mjs connect "$electron" "$rust" "$tools/evidence/identity"
sudo --login --user office-rust-ci /usr/bin/env PATH="$tools:/usr/local/bin:/usr/bin:/bin" \
  TAURI_DRIVER="$tools/tauri-driver" TAURI_NATIVE_DRIVER="$(command -v WebKitWebDriver)" \
  sh "$tools/desktop/comparison/linux-handoff.sh" "$electron" "$rust" "$tools/evidence/handoff"
sudo apt-get remove -y ai-agent-office-desktop
test ! -e "$electron"
test -x "$rust"
verify verify-connection.mjs rust-survivor "$electron" "$rust" "$tools/evidence/identity"
sudo apt-get install -y --no-install-recommends ./electron-installer/*.deb
sudo apt-get remove -y ai-agent-office-rust
test ! -e "$rust"
test -x "$electron"
verify verify-connection.mjs survivor "$electron" "$rust" "$tools/evidence/identity"
