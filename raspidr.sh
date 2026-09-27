#!/usr/bin/env bash
# RaspiDR services on the Pi: the knob (src/knob.py) and the assistant (src/assistant.py) as systemd units.
# Runs on the Pi from the project directory (deploy.sh ships it with the code); uses sudo for systemctl.
#   ./raspidr.sh install     Python dependencies into .venv + journal settings + PulseAudio off + memory cgroup
#                            + install, enable and start both units
#   ./raspidr.sh uninstall   stop, disable and remove the units, the journal settings and the PulseAudio mask
#                            (.venv, settings and cgroup_enable=memory stay)
#   ./raspidr.sh journal     only the journal settings: kept on disk, 1 month max, 64 MB max
#   ./raspidr.sh start | stop | restart | status
#   ./raspidr.sh logs [-f]   journal of both units (-f — follow)
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
USER_NAME="$(id -un)"
USER_ID="$(id -u)"
UNITS=(raspidr-knob raspidr-assistant)  # start order: the knob owns the ring and the encoder
UNIT_DIR=/etc/systemd/system
JOURNAL_CONF=/etc/systemd/journald.conf.d/raspidr.conf
CMDLINE=/boot/firmware/cmdline.txt
PULSE_NOTE="$HOME/PULSEAUDIO_DISABLED.txt"

if [[ "$USER_ID" == 0 ]]; then
  echo "run as the regular user, not root: the services run as that user and sudo is called where needed" >&2
  exit 1
fi

write_unit() {  # name, description, script, extra [Unit] lines, extra [Service] lines
  sudo tee "$UNIT_DIR/$1.service" >/dev/null <<EOF
[Unit]
Description=$2
$4

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -u src/$3
Restart=always
RestartSec=3
TimeoutStopSec=10
$5

[Install]
WantedBy=multi-user.target
EOF
}

install_deps() {
  [[ -d "$DIR/.venv" ]] || python3 -m venv --system-site-packages "$DIR/.venv"
  "$DIR/.venv/bin/python" -m pip install -q -r "$DIR/requirements.txt"  # -m: the venv was moved, script shebangs are stale
  "$DIR/.venv/bin/python" -c 'import openwakeword.utils as u; u.download_models()' >/dev/null 2>&1
  echo "dependencies are up to date"
}

install_units() {
  # MemoryMax: past it the kernel reclaims/kills inside the unit instead of dragging the whole Pi into swap
  # (works only with the memory cgroup on — see enable_memory_cgroup)
  # knob: ~15 MB, +~20 MB with bleak for the Triki token
  write_unit raspidr-knob "RaspiDR knob: encoder, LED ring, volume, battery, Triki token" knob.py \
    "After=pigpiod.service wm8960-soundcard.service bluetooth.service
Wants=pigpiod.service" "MemoryMax=96M"
  # XDG_RUNTIME_DIR: the assistant stops the user's PulseAudio (it grabs the sound card) via systemctl --user
  write_unit raspidr-assistant "RaspiDR voice assistant" assistant.py \
    "After=network-online.target wm8960-soundcard.service raspidr-knob.service
Wants=network-online.target raspidr-knob.service" \
    "Environment=XDG_RUNTIME_DIR=/run/user/$USER_ID
MemoryMax=320M"
  sudo systemctl daemon-reload
  sudo systemctl enable "${UNITS[@]/%/.service}" >/dev/null 2>&1
  echo "units installed and enabled: ${UNITS[*]}"
}

install_journal() {
  # system-wide: Raspberry Pi OS keeps the journal in RAM only (Storage=volatile), so it's gone after a reboot
  # or a hang + power cycle; this drop-in sorts after the rpi one and wins
  sudo mkdir -p "$(dirname "$JOURNAL_CONF")"
  printf '%s\n' "[Journal]" "Storage=persistent" "MaxRetentionSec=1month" "MaxFileSec=1week" "SystemMaxUse=64M" \
    | sudo tee "$JOURNAL_CONF" >/dev/null
  sudo systemctl restart systemd-journald
  sudo journalctl --flush  # move what's in RAM to /var/log/journal now (at boot systemd does it itself)
  echo "journal: on disk, 1 month / 64 MB max"
}

disable_pulseaudio() {
  # PulseAudio is socket-activated by every login and takes the card: the microphone then records pure zeros
  systemctl --user mask --now pulseaudio.socket pulseaudio.service >/dev/null 2>&1 || true
  cat >"$PULSE_NOTE" <<'NOTE'
PulseAudio is disabled (masked) for this user by RaspiDR (raspidr.sh install).

Why: it starts on every login (socket activation), grabs the WM8960 sound card, and the
assistant's microphone then records pure zeros — the wake word stops working.

Turn it back on:
    systemctl --user unmask pulseaudio.socket pulseaudio.service
    systemctl --user start pulseaudio.socket
(./raspidr.sh uninstall does the same and removes this file.)
NOTE
  echo "PulseAudio masked (note: $PULSE_NOTE)"
}

enable_memory_cgroup() {
  # the Raspberry Pi firmware adds cgroup_disable=memory; a later cgroup_enable=memory wins. Needs a reboot.
  if ! grep -qw "cgroup_enable=memory" "$CMDLINE"; then
    sudo cp "$CMDLINE" "$CMDLINE.bak"
    sudo sed -i '1 s/$/ cgroup_enable=memory/' "$CMDLINE"
    echo "memory cgroup enabled in $CMDLINE (backup: $CMDLINE.bak) — reboot for MemoryMax to take effect"
  elif ! grep -qw memory /sys/fs/cgroup/cgroup.controllers; then
    echo "memory cgroup is set in $CMDLINE but not active yet — reboot"
  fi
}

stop_all() {
  sudo systemctl stop "${UNITS[@]}" 2>/dev/null || true
  # copies started by hand (nohup, with or without -u) would fight the units for the ring and the microphone;
  # [k]nob / [a]ssistant — so the pattern doesn't match a command line that contains it (ssh, this shell)
  local pat="python[0-9.]* (-u )?src/([k]nob|[a]ssistant)\.py"
  pkill -f "$pat" || true
  for _ in 1 2 3 4 5; do
    pgrep -f "$pat" >/dev/null || break
    sleep 1
  done
}

installed() {
  [[ -f "$UNIT_DIR/raspidr-assistant.service" ]] || { echo "not installed: ./raspidr.sh install" >&2; exit 1; }
}

status() {
  systemctl --no-pager --lines=0 status "${UNITS[@]}" || true
  echo
  ps -eo rss=,args= | awk '/python -u src\/(knob|assistant)\.py/ && !/awk/ {printf "%4d MB  %s\n", $1 / 1024, $NF}'
}

case "${1:-}" in
  install)
    stop_all  # first: pip next to a running assistant is too much for 416 MB
    install_deps
    install_journal
    disable_pulseaudio
    enable_memory_cgroup
    install_units
    sudo systemctl start "${UNITS[@]}"
    status
    ;;
  uninstall)
    sudo systemctl disable --now "${UNITS[@]}" 2>/dev/null || true
    for u in "${UNITS[@]}"; do sudo rm -f "$UNIT_DIR/$u.service"; done
    sudo systemctl daemon-reload
    if [[ -f "$JOURNAL_CONF" ]]; then
      sudo rm -f "$JOURNAL_CONF"
      sudo systemctl restart systemd-journald
    fi
    systemctl --user unmask pulseaudio.socket pulseaudio.service >/dev/null 2>&1 || true
    rm -f "$PULSE_NOTE"
    echo "units, journal settings and the PulseAudio mask removed (.venv, ~/.config/raspidr, the journal itself"
    echo "and cgroup_enable=memory in $CMDLINE stay)"
    ;;
  journal)
    install_journal
    ;;
  start)
    installed
    sudo systemctl start "${UNITS[@]}"
    ;;
  stop)
    installed
    sudo systemctl stop "${UNITS[@]}"
    ;;
  restart)
    installed
    stop_all
    sudo systemctl start "${UNITS[@]}"
    ;;
  status)
    status
    ;;
  logs)
    shift
    journalctl --no-pager -n 40 -o short -u raspidr-knob -u raspidr-assistant "$@"
    ;;
  *)
    sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
