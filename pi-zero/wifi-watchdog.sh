#!/bin/sh
# Reconnect WiFi if wlan0 is down. A failed WPA handshake at boot makes NetworkManager mark the
# connection as "no secrets" and stop retrying it, so without this the Pi stays offline.
# Runs from doorspy-wifi-watchdog.timer.
state=$(nmcli -g GENERAL.STATE device show wlan0 2>/dev/null | cut -d' ' -f1)
case "$state" in
    100) exit 0 ;;                  # connected
    4[0-9]|5[0-9]|6[0-9]|7[0-9]|8[0-9]|9[0-9]) exit 0 ;;   # activation in progress
esac
echo "wlan0 state '${state:-unknown}', reconnecting"
# "device connect" picks the best known network and clears NetworkManager's autoconnect block
exec nmcli --wait 60 device connect wlan0
