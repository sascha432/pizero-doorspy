# Setup

This goes from a blank SD card to a working camera, and lists every problem hit along the way.

## Hardware

- **Raspberry Pi Zero W v1.1.** It's ARMv6 with a single core and 512 MB of RAM (426 MB usable). Its WiFi is **2.4 GHz only** and supports **WPA2 only**.
- **Camera with an OV5647 sensor** (Camera Module v1 or a compatible clone), connected with a **Zero camera cable** (22-pin to 15-pin).
- **A 5 V / 2 A power supply** into the **PWR** port. The middle **USB** port is the data port for the USB gadget fallback.

## 1. Flash the SD card

In Raspberry Pi Imager, choose **Raspberry Pi Zero** and **Raspberry Pi OS Lite (32-bit)**. The image used here was trixie, 2026-10-06. Settings in the customisation step:

| Setting | Value |
|---|---|
| Hostname | `doorspy` |
| User / password | `pispy` / `password` (the setup default; `tools/deploy.sh` relies on it) |
| SSH | on, password login |
| WiFi | set it, but **don't rely on it** (see below) |
| WiFi country | CA |
| Time zone / keyboard | America/Vancouver / de |

> **Imager's WiFi settings get silently dropped by this image.** On the first boot, cloud-init reads
> `network-config` but can't apply the WiFi part, and the log shows
> `Could not find module named cc_netplan_nm_patch`. Only the wired/USB connection is created.
> So plan to get in over the USB gadget (step 2) and add WiFi with `nmcli` (step 4).

## 2. Turn on the USB gadget fallback

On the card's `bootfs` partition:

- `config.txt`: add `dtoverlay=dwc2` at the end, under `[all]`.
- `cmdline.txt`: add `modules-load=dwc2,g_ether` right after `rootwait`. The file must stay a single line.

The Pi then shows up on the PC as **"Raspberry Pi USB Gadget"** (`USB\VID_2E8A&PID_0013`) when you connect its **USB** port to the PC.

## 3. First connection over USB (Windows)

1. **Driver:** Windows binds the gadget to the serial driver ("USB Serial Device (COMx)"), which doesn't work for this. Fix it from an elevated PowerShell:
   `tools\windows\force-rndis.ps1`. It installs the built-in **Remote NDIS Compatible Device** driver. You need to do this once for each USB port.
2. **DHCP:** with no DHCP server answering on the USB link, NetworkManager on the Pi keeps taking `usb0` down and up again, so the link flaps between Up and Disconnected. Run `tools\windows\enable-ics.ps1` (elevated) to share the PC's Wi-Fi with that adapter. The Pi then gets a `192.168.137.x` address and internet access.
3. Run `ssh pispy@doorspy.local`, or connect to the `192.168.137.x` address that Windows shows under `Get-NetNeighbor -InterfaceAlias "Ethernet 3"`.

## 4. WiFi

Add the connections by hand. If you add a 64-character hex PSK (WPA2's hashed form of the password), the plain password isn't stored:

```bash
# PSK = PBKDF2-HMAC-SHA1(password, ssid, 4096, 32) as hex; e.g. on the PC:
#   python -c "import hashlib;print(hashlib.pbkdf2_hmac('sha1',b'PASSWORD',b'SSID',4096,32).hex())"
sudo nmcli con add type wifi ifname wlan0 con-name FoxSay2 ssid WhatDoesTheFoxSay2 \
     wifi-sec.key-mgmt wpa-psk wifi-sec.psk <64-hex-psk> \
     wifi.powersave 2 connection.autoconnect-priority 20
sudo nmcli con up FoxSay2
```

The networks currently configured:

| Connection | SSID | Priority | Gets |
|---|---|---|---|
| `FoxSay2` | WhatDoesTheFoxSay2 (WPA2, channel 3) | 20 | 192.168.0.x, same LAN as the PC |

`WhatDoesTheFoxSay?` doesn't work. Its 5 GHz band is out of the Pi's range, and its 2.4 GHz band is WPA3-only or mixed mode with WPA3 required.

## 5. Install the camera software

Run this from this repo on the PC (Git Bash):

```bash
tools/deploy.sh pispy@doorspy.local
```

It copies `pi-zero/` over and runs `install.sh`, which:

- installs the apt packages (`python3-picamera2`, `python3-av`, `python3-simplejpeg`, `python3-numpy`, the GPIO/MQTT ones, `openssl`)
- installs MediaMTX into `/opt/mediamtx`
- copies the files into place
- creates the web UI's login (`admin` / `password`), the stream login for port 8080, RTSP and WebRTC (`guest` / `password`) and the self-signed certificate if they don't exist yet
- turns WiFi power saving off
- speeds up booting: disables cloud-init after the first boot and replaces the netplan-backed USB connection with a plain NetworkManager keyfile (WiFi up after about 2¼ min before this)
- turns on the serial console in `config.txt`
- enables and starts both services, plus the WiFi watchdog timer

The image has no SFTP server, so `scp` fails with "Connection closed". `deploy.sh` streams a tar archive over `ssh` instead.

`sudo` asks for the `pispy` password, because Imager writes `sudo: null`, which means no NOPASSWD rule. `deploy.sh` passes the default password `password` to `sudo -S` itself. If you've changed it, run `DOORSPY_SUDO_PASS='…' tools/deploy.sh`.

## 6. Web UI

Open **`https://doorspy.local:8443/`**. The certificate is self-signed, so the browser warns once; accept it. Log in as **`admin` / `password`** (the setup default, like ssh) and change the password on the **System** tab straight away (anyone on the LAN can reach the page). Everything the UI does is described in [configuration](configuration.md#web-ui).

## Access

The login is `pispy` with the default password `password`, used for password login and `sudo`. Anyone on the LAN who knows it can log in. To allow only keys over SSH, run `sudo sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && sudo systemctl reload ssh`. `sudo`, and with it `deploy.sh`, keeps using the password.

`~pispy/.ssh/authorized_keys` holds:

- `sascha@pc` (ED25519), the key in `C:\Users\sascha\.ssh\id_ed25519`
- `rsa-key-20171017` (RSA 4096). It needs a client that signs with rsa-sha2-256/512, such as PuTTY 0.75 or later or a current OpenSSH, because SHA-1 `ssh-rsa` signatures are refused.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| No DHCP request in the router after flashing | The cloud-init WiFi bug (step 1), a 5 GHz or WPA3-only network, or a WiFi country that blocks the router's channel. Get in over USB and use `nmcli`. |
| Pi shows up as a COM port | Run `force-rndis.ps1` (step 3). |
| USB link keeps dropping | No DHCP on the link. Run `enable-ics.ps1` (step 3). |
| `scp: Connection closed` | There's no SFTP server. Use `tar … \| ssh …` the way `deploy.sh` does. |
| Changing `network-config` on the card has no effect | cloud-init runs once per instance-id. Change `instance-id` in `meta-data` **and** `i=` in `cmdline.txt`. Even then the WiFi part doesn't apply (step 1). |
| Choppy or laggy stream | Check `vcgencmd get_throttled`, which should be `0x0`; anything else means undervoltage. Check WiFi power saving with `nmcli -f 802-11-wireless.powersave con show FoxSay2`, which should be `2`. |
| WiFi takes minutes after power-on, or stays down | Check `systemd-analyze blame` and `journalctl -b -u NetworkManager -u wpa_supplicant`. A `4-Way Handshake failed … WRONG_KEY` at boot with the right key is a timeout; `doorspy-wifi-watchdog.timer` reconnects within 2 minutes. |
| No network at all | Serial console: USB-UART adapter RX → pin 8 (GPIO14), TX → pin 10 (GPIO15), GND → pin 6, 115200 baud. Needs `enable_uart=1` in `config.txt`, which you can also add from the PC on the card's `bootfs` partition. |
| Camera service logs | `journalctl -u doorspy-cam -f` and `journalctl -u mediamtx -f`, or the System tab of the web UI |
| A stream client gets `401` from port 8080 | The stream needs a login since 2026-10-20: `guest` / `password` by default (System tab to change). `curl -u guest:password http://doorspy.local:8080/api/snapshot.jpeg`. |
| RTSP / WebRTC client gets `401` | Since 2026-10-25 they need the same login: `rtsp://guest:password@doorspy.local:8554/cam`. If even a correct login fails, `journalctl -u mediamtx` shows why the check on `127.0.0.1:8081` failed (`camera.py` not running?). Frigate on the Pi 4 needs the login too, see [pi4.md](pi4.md#the-zeros-stream-login). |
| Web UI: browser keeps asking for a login | Wrong password. Forgot it: `sudo rm /var/lib/doorspy/web.auth` and redeploy, which recreates `admin` / `password`. |
| Web UI: certificate error after the IP changed | The certificate names `doorspy.local` and the IP at install time. Use the name, or `sudo rm /etc/doorspy/web.key` and redeploy. |
| Web UI: "doorspy-io not running" on the Lights / Sensors tabs | `systemctl status doorspy-io` and `journalctl -u doorspy-io`. The socket is `/run/doorspy/io.sock`; it must be owned by `pispy`. |
| Web UI: nothing on :8443 but :8080 works | `journalctl -u doorspy-cam | grep "web UI"`: a missing cert or auth file disables the UI, the camera keeps running. Redeploy. |
