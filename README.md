# Raven

Control your own Windows PCs and Macs from each other, **over your local network only**.

- **See the remote desktop** live and **control its mouse and keyboard**
- **Sleep, lock, restart or shut down** a remote computer with one click
- **Wake** a sleeping or powered-off computer with Wake-on-LAN
- Finds other Raven computers on the network automatically (Bonjour/mDNS)
- One app on every machine. Each computer can control others, be controlled, or both

Nothing goes through the internet: there are no accounts, cloud relays or port forwarding.
The host refuses any connection that doesn't come from a private LAN address.

## How it works

```
 Controller (any OS)                                   Host (PC or Mac)
┌──────────────────┐   TLS WebSocket, port 47470     ┌────────────────────────┐
│ Viewer window    │ ◀── JPEG frames (mss) ───────── │ screen capture         │
│ mouse / keyboard │ ─── input events ─────────────▶ │ pynput input injection │
│ power buttons    │ ─── sleep / shutdown ─────────▶ │ OS power commands      │
└──────────────────┘ ◀── Bonjour "_raven._tcp" ──────└────────────────────────┘
        └──── Wake-on-LAN magic packet (UDP broadcast) ──▶ powered-off host
```

Built with Python, PySide6 (Qt) for the UI, `websockets` + TLS for transport, `mss` for
screen capture, `pynput` for mouse and keyboard, and `zeroconf` for discovery.

## Install and run (from source)

You need Python 3.10 or newer on each computer.

```bash
git clone <this repo> raven && cd raven
python -m venv .venv
# Windows: .venv\Scripts\activate      macOS: source .venv/bin/activate
pip install -e .
raven            # or: python -m raven
```

Ready-made builds: the `build` GitHub Actions workflow produces `Raven-Windows.zip`
(`Raven.exe`) and `Raven-macOS.zip` (`Raven.app`) as artifacts. To build locally:

```bash
pip install -e ".[dev]"
pyinstaller --noconfirm packaging/raven.spec     # → dist/Raven/ or dist/Raven.app
```

## Set up

### On each computer you want to control (the host)

1. Open Raven → **This computer** tab.
2. Tick **Allow other computers on my network to control this computer** and choose a
   password.
3. Optional: tick **Start Raven when I log in** so the computer is reachable again after
   a restart. Closing the window keeps Raven running in the tray/menu bar while remote
   control is on.

**macOS:** grant Raven (or Terminal/Python when running from source) both
**Screen Recording** and **Accessibility** in *System Settings → Privacy & Security*, then
restart Raven. The tab has buttons that open those pages. The first remote *Shut down* or
*Restart* also asks you to allow Raven to control *System Events*.

**Windows:** allow Raven through Windows Defender Firewall on **Private** networks when
prompted.

### On the computer you control from (the controller)

1. Open Raven → **Other computers**. Hosts on the network appear automatically. Use
   **Add…** to enter an IP address if one doesn't show up.
2. Select a computer and press **Connect** to see and control it, or use **Sleep**,
   **Lock**, **Restart**, **Shut Down** directly.
3. The first time, Raven shows the host's fingerprint. Check that it matches the one on
   that computer's *This computer* tab. Then enter the password, which can be remembered.

In the viewer window:

| Control | What it does |
|---|---|
| Display | Pick which monitor to view |
| Fast / Balanced / Sharp | Trade image quality for smoothness |
| View only | Watch without sending mouse or keyboard input |
| Swap ⌘/Ctrl | Mac Command ↔ PC Control, so ⌘C copies on a PC (on by default between a Mac and a PC) |
| Type clipboard | Types your local clipboard text on the remote computer |
| Power | Sleep / Lock / Restart / Shut Down |

### Wake-on-LAN (turning computers back on)

After the first successful connection Raven remembers the host's MAC address, and the
**Wake** button becomes available. The host must allow it:

- **Windows PC:** enable *Wake on LAN* / *Power on by PCI-E* in the BIOS/UEFI, and in
  *Device Manager → network adapter → Power Management* tick *Allow this device to wake
  the computer* and *Only allow a magic packet*. Wired Ethernet is the most reliable.
  Waking from full shutdown often requires turning off *Fast startup*.
- **Mac:** enable *Wake for network access* in *System Settings → Battery/Energy*. Macs
  wake from sleep, but not from a full shutdown.

### Command line

```bash
raven host                       # run only the host, no window (prompts for a password first time)
raven power 192.168.1.20 sleep   # sleep | lock | restart | shutdown
raven wake a4:83:e7:12:34:56     # send Wake-on-LAN
```

## Security

- **LAN only.** Connections from non-private addresses are rejected before the
  handshake. Allowed: 10/8, 172.16/12, 192.168/16, link-local, loopback, IPv6 ULA.
- **Encrypted.** All traffic uses TLS with a per-computer certificate generated on first
  run. Controllers pin each host's certificate fingerprint and warn loudly if it changes.
- **Password never sent.** Login is a challenge–response
  (HMAC of a fresh nonce with a PBKDF2-derived key), bound to the TLS certificate so it
  can't be relayed. The host stores only the derived key, and "remember password" stores
  the derived key, not the password.
- **Brute-force protection.** After 5 wrong passwords, that address is locked out for a
  minute.
- **Host stays in charge.** The host can turn off mouse/keyboard control or power
  commands, sees an activity log with a notification for every connection, and can
  disconnect everyone.

Settings and certificates live in `%APPDATA%\Raven` (Windows),
`~/Library/Application Support/Raven` (macOS) or `~/.config/raven` (Linux).

## Known limitations

- Windows: input can't reach elevated (admin) windows or the UAC / Ctrl+Alt+Del secure
  desktop unless Raven itself runs as administrator. On machines with hibernation
  enabled, *Sleep* hibernates. Run `powercfg /hibernate off` for true sleep.
- macOS: *Lock* turns the display off. This locks the Mac when "Require password after
  screen saver begins or display is turned off" is set to *Immediately* (the default).
  System-wide shortcuts such as ⌘Tab or ⌘Space go to your local Mac, not the remote one.
- The host must be logged in, because Raven runs in the user's session. The login
  screen after a restart can't be controlled yet.
- No audio or file transfer yet.

## Development

```bash
pip install -e ".[dev]"
pytest            # on headless Linux: xvfb-run -a pytest
```

```
raven/
  host/server.py       host: TLS WebSocket server, auth, streaming, input, power
  host/service.py      server + Bonjour advertisement
  client/session.py    controller: connect, verify fingerprint, authenticate
  platform/            capture (mss), inputs (pynput), power, permissions, netinfo
  gui/                 Qt app: main window, host tab, remote tab, viewer
  protocol.py          message and frame formats
  security.py          certificates, password proofs, LAN checks
  discovery.py, wol.py, autostart.py, cli.py
```
