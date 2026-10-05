# claude-token-meter

A tiny desk display that shows how much of your **Claude subscription usage limit** you've used — the rolling **5-hour** window and the **weekly (7-day)** window — at a glance, without opening a browser.

[한국어 README](README-KR.md)

<p>
  <img src="docs/images/meter.png" alt="Meter: 5H 47% resets in 2h13m, 7D 21% resets in 3d04h" width="360">
  <img src="docs/images/meter-busy.png" alt="Meter running ahead of pace: 5H 83% orange, 7D 61% amber" width="360">
</p>

It runs on a [LilyGO T-Display](https://www.lilygo.cc/products/lilygo-t-display-esp32-1-14-inch) (classic ESP32, 240×135 IPS) and gets its numbers from **Claude Code on your Mac**. It makes **zero extra requests to Anthropic** and never touches your Claude credentials.

## How it reads your usage

Claude Code passes a JSON blob to its [status line](https://code.claude.com/docs/en/statusline) command on every assistant message. For Pro and Max subscribers that blob includes `rate_limits.five_hour` and `rate_limits.seven_day`, each with `used_percentage` and `resets_at`. This project registers a tiny status-line script that saves those numbers locally. A background bridge on your Mac then pushes them to the device over USB or WiFi.

```
Claude Code sessions ──stdin JSON──▶ host/ctm/statusline.py   (~20 ms, no network)
                                        │ one file per session
                                        ▼
                        ~/.claude-token-meter/sessions/*.json
                                        │
                                        ▼
                   bridge (LaunchAgent com.ctm.bridge, Python)
                     ├─ merge: per window, latest reset wins, same window → max %
                     ├─ send: on change + heartbeat every 120 s, resend on wake
                     └─ USB serial if the board is plugged in, else WiFi
                        (mDNS + HTTP POST signed with HMAC-SHA256)
                                        ▼
                     T-Display firmware: verify → state → render
```

What follows from this design:

- **The numbers update only while you use Claude Code.** Usage on claude.ai or the desktop/mobile apps counts toward the same limits. It appears on the meter with your next Claude Code message. The footer always shows how old the data is.
- **Concurrent sessions are handled.** Different Claude Code sessions report different values, because each one remembers its own last response. The bridge merges them: within the same window, usage never goes down, so it takes the maximum.
- **No Anthropic login, no API key, no polling.** The Mac only talks to the device on your LAN or over USB.

## Reading the screen

| Element | Meaning |
|---|---|
| Big number | 5-hour window usage (%) and time until it resets |
| Small number | 7-day window usage (%) and time until it resets |
| Bars | Usage of each window |
| Cyan tick on a bar | **Pace**: how much of the window's time has elapsed. A bar past its tick means you're using the limit faster than it refills. |
| Colors | Neutral at or below pace, **amber** above pace, **orange** ≥ 80 %, **red** ≥ 95 % |
| Footer dot | Green: data arrived ≤ 150 s ago. Grey: older. Red + "host offline": nothing for > 5 min. "USB" / "WiFi" shows how the last update arrived. |
| `restored` | Shown after power-on with the last saved values, until the Mac sends fresh data. Times show `--` until the clock is set. |
| `0% · reset` (grey) | That window's reset time has passed. It's an estimate until Claude Code sees a new response. |

**Buttons:**

- **BTN1** (GPIO35, short press) opens a detail view: reset times in local time, WiFi/IP/RSSI, uptime, heap, and reconnect counters. It returns to the meter after 15 s.
- **BTN2** (GPIO0) cycles the backlight between 100 %, 40 % and 10 %.
- The backlight dims by itself after 60 min without changes.

<img src="docs/images/detail.png" alt="Detail view" width="360">

## Requirements

- **macOS.** The bridge uses launchd, Keychain and mDNS. Linux would need a port of `ctm install` and the keystore.
- **Claude Code with a Pro or Max subscription**, recent enough to include `rate_limits` in the status-line JSON. It was observed present in 2.1.289 and absent in 2.1.283/2.1.284.
- **LilyGO T-Display, classic ESP32**, not the S3. The partition table assumes the **16 MB flash** variant (`firmware/partitions_16mb.csv`). For a 4 MB board, drop the two `board_upload.flash_size` / `board_build.partitions` lines in `platformio.ini`; the app is about 1 MB.
- [PlatformIO](https://platformio.org/). Its bundled Python (`~/.platformio/penv/bin/python3`) already has `pyserial`, which the bridge needs.
- `/usr/bin/python3` (Xcode Command Line Tools) for the status-line script.
- A 2.4 GHz WiFi network that the Mac and the board share. WiFi is optional; USB-only works too.

## Quick start

All host commands run from `host/`. To save typing:

```sh
cd host
alias ctm='~/.platformio/penv/bin/python3 -m ctm.cli'
```

**1. Select your board.** Plug the T-Display in by USB, then run:

```sh
ctm select-board
```

This lists USB-serial ports with their serial numbers and chips and saves your choice to `~/.claude-token-meter/config.json`. The bridge only ever opens **that** board, matched by USB serial number. Other ESP32 boards on your desk are never touched.

**2. Flash the firmware.**

```sh
cd .. && ~/.platformio/penv/bin/pio run -d firmware -e t-display -t upload \
  --upload-port "$(cd host && ~/.platformio/penv/bin/python3 -m ctm.cli port)"
cd host
```

**3. Install the bridge and the status line.**

```sh
ctm install --statusline --dry-run   # shows exactly what will change
ctm install --statusline
```

- This writes `~/Library/LaunchAgents/com.ctm.bridge.plist`. The bridge starts at login and is restarted if it exits.
- It sets `statusLine` in `~/.claude/settings.json` and saves a timestamped backup first.
- **If you already have a status line**, install stops and shows it. `--force` replaces it. Your terminal status bar then shows `5h 47% · 7d 21%`.

**4. Pair over WiFi (optional).** Run this in a **regular terminal** (Terminal or iTerm). It asks for a password, so it won't work through Claude Code's `!` prefix.

```sh
ctm setup
```

- The board scans for networks. You pick one and type the password.
- A random 32-byte pairing key is created and stored in your **macOS Keychain** and on the board.
- After this you can unplug the USB cable and power the board from any USB charger. Updates continue over WiFi. If you plug it back into the Mac, the bridge switches to USB automatically.

**5. Check everything.**

```sh
ctm doctor
```

Send a message in any Claude Code session and the meter updates within seconds.

**Uninstall:** `ctm uninstall --statusline` removes the LaunchAgent and our status line. `ctm unpair` erases WiFi settings and the key from the board and from the Keychain.

## Privacy & security

- **No Claude credentials are read.** Claude Code's OAuth token and the Keychain item `Claude Code-credentials` are never read or used. The device holds no Anthropic credentials.
- **No extra traffic to Anthropic.** The only network traffic is Mac ↔ device on your LAN.
- **What the device receives:** usage percentages, reset times and observation times. Nothing else from your sessions — no prompts, paths or session IDs.
- **WiFi pushes are signed.** Each push carries HMAC-SHA256 with the pairing key and a monotonic sequence number. The device rejects:
  - unsigned or forged requests (`401`)
  - replays (`409`)
  - bodies over 1 KB (`413`)
- **Where secrets live.** The WiFi password is stored only on the device (NVS). The pairing key lives in the device's NVS and in your Keychain (`ctm-pairing`). Neither is ever printed or logged.
- `GET /v1/info` on the device is unauthenticated and contains no secrets.

## Troubleshooting

- **Start with `ctm doctor`.** It checks, PASS/FAIL each:
  - status-line registration
  - session files
  - the selected board
  - the bridge process and LaunchAgent
  - the transport
  - device reachability
  - the pairing key
  - the last acknowledgement
- **The meter shows `--`.** Claude Code only includes `rate_limits` after the first response in a session, and only on subscription plans. Send a message.
- **Local Network permission.** If the bridge log shows `EHOSTUNREACH`, allow the Python that runs the bridge under System Settings → Privacy & Security → Local Network.
- **Use `curl -4` to talk to the device.** Plain `curl http://ctmeter-xxxx.local/...` may try IPv6 first and time out. The bridge uses IPv4 only.
- **Re-flashing while the bridge holds the port.** The bridge keeps the board's serial port open exclusively (TIOCEXCL) so other tools can't grab it by accident. Release it with `kill -USR1 "$(cat ~/.claude-token-meter/serial/hold.pid)"`, flash, then run the same command again to re-acquire.
- **Logs:**
  - `~/.claude-token-meter/serial/YYYYMMDD.log`: device output plus bridge events, kept 14 days.
  - `~/.claude-token-meter/launchd/`: launchd stdout/stderr.

## Known limitations

- **Only the two windows above are shown.** Claude Code's status-line JSON exposes `five_hour` and `seven_day`, not per-model weekly limits.
- **Numbers refresh only on Claude Code activity**, as described above.
- **No over-the-air firmware updates yet.** Re-flash over USB.
- **Device screen text is English only.** The built-in TFT fonts have no Hangul.

## Development

- `docs/ARCHITECTURE.md`: design and rationale
- `docs/PROTOCOL.md`: the host ↔ device contract
- `docs/HARDWARE.md`: board facts and gotchas, read before touching firmware
- `docs/P0_FINDINGS.md`: what was measured on real hardware
- `docs/TASKS.md`: open items
- **Tests:** `bash tools/check_fixtures.sh` runs the host unit tests and the firmware's native tests (`pio test -e native`). It fails closed unless both sides agree on every shared fixture in `fixtures/`.
- **Screen preview without a device:** `~/.platformio/penv/bin/python3 tools/preview.py <frame.json> -o out.png`
- **Working with Claude Code:** [`CLAUDE.md`](CLAUDE.md) has the working rules this project was built with.

## License

[MIT](LICENSE)
