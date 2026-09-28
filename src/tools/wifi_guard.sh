#!/usr/bin/env bash
# Boot-time Wi-Fi guard, run as root once per boot by the raspidr-wifi-guard unit (raspidr.sh install).
# Now and then the firmware download to the BCM43430 fails at boot ("brcmfmac: ... Downloaded RAM image is
# corrupted", "dongle nvram file download failed"): wlan0 never appears, the wake word still works, but every
# request dies on DNS. If wlan0 is missing WAIT_S after start: reload the driver; if it's still missing, reboot —
# at most once per REBOOT_GAP_S, so a chip that is really dead can't put the Pi into a reboot loop.
set -uo pipefail
WAIT_S=45
RELOAD_WAIT_S=30
REBOOT_GAP_S=3600
MARK=/var/lib/raspidr/wifi-guard-reboot

wait_wlan() {  # seconds
  for ((i = 0; i < $1; i++)); do
    [[ -e /sys/class/net/wlan0 ]] && return 0
    sleep 1
  done
  return 1
}

if wait_wlan "$WAIT_S"; then
  echo "wlan0 is present"
  exit 0
fi

echo "wlan0 missing ${WAIT_S} s after start — reloading brcmfmac"
modprobe -r brcmfmac_wcc brcmfmac
sleep 2
modprobe brcmfmac
if wait_wlan "$RELOAD_WAIT_S"; then
  echo "wlan0 is back after the driver reload"
  exit 0
fi

# the clock right after boot is fake-hwclock's (restored from the last save): if it went backwards the age
# comes out negative, which also counts as "too recent" — safer to skip the reboot than to loop
if [[ -f "$MARK" ]] && (( $(date +%s) - $(stat -c %Y "$MARK") < REBOOT_GAP_S )); then
  echo "wlan0 still missing, but the guard already rebooted less than ${REBOOT_GAP_S} s ago — giving up"
  exit 1
fi
echo "wlan0 still missing after the driver reload — rebooting"
mkdir -p "$(dirname "$MARK")"
touch "$MARK"
systemctl reboot
