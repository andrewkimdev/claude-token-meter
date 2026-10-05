# host — ctm (P4: USB serial + WiFi, P5 robustness, P6 install)

Implements `docs/PROTOCOL.md` v1. Files are under `~/.claude-token-meter/` (override with `CTM_HOME`).

| file | role |
|---|---|
| `ctm/statusline.py` | Claude Code statusLine command. Writes `sessions/<session_id>.json` (§1). Self-contained, stdlib only. |
| `ctm/merge.py` | Merge rules (§2) and pruning of session files older than 8 days |
| `ctm/proto.py` | Builds state frames (§3) and parses `@` lines. Also holds the reference device acceptance rules used by the fixtures. |
| `ctm/bridge.py` | Long-running bridge. Owns the serial port (USB) or POSTs signed frames (WiFi). |
| `ctm/wifi.py` | WiFi transport (§4.2): HMAC signing, mDNS/cached-IP resolution, HTTP status mapping, backoff |
| `ctm/board.py` | Which USB board is ours: `config.json` `usb_serial`, resolution order, `select-board` |
| `ctm/device.py` | `device.json` (host, last IP, paired_at; never the key) |
| `ctm/keystore.py` | Pairing key in the macOS Keychain via `security` (service `ctm-pairing`, account = host) |
| `ctm/pairing.py` | `setup`, `unpair`, `info` over USB (§4.3) |
| `ctm/install.py` | `install` / `uninstall`: LaunchAgent `com.ctm.bridge` + statusLine in `~/.claude/settings.json` |
| `ctm/doctor.py` | `doctor`: PASS/FAIL/SKIP health report (incl. `board`: configured serial + present), verdict only if every check ran |
| `ctm/cli.py` | `status`, `select-board`, `port`, `push-test`, `setup`, `unpair`, `info`, `doctor`, `install`, `uninstall` |

## Register the statusLine

In `~/.claude/settings.json`:

```json
"statusLine": {"type": "command",
               "command": "/usr/bin/python3 -S /path/to/claude-token-meter/host/ctm/statusline.py"}
```

(`ctm install --statusline` writes this for you with the absolute path of your checkout.)

The script prints `5h 52% · 7d 29%`. It prints `--` when a value is unknown and `0%` once that window's `resets_at` has passed.

## Select your board

The bridge, `setup`, `unpair`, `info`, `port` and `tools/serial_hold.py` only ever touch
**one** board, identified by its USB serial number. There is no built-in default:

1. explicit flag (`--port-serial N`; the bridge takes `--usb-serial N`)
2. env `CTM_USB_SERIAL` (the LaunchAgent does not set it)
3. `~/.claude-token-meter/config.json` → `{"usb_serial": "..."}` (written by `ctm select-board`)
4. none → USB is off. The bridge runs WiFi-only (if paired) and logs
   "no board selected — run `ctm select-board`" once. It never opens "some" port:
   with several ESP32 boards on one Mac that would grab (and reset) the wrong one.

```sh
cd host
P=~/.platformio/penv/bin/python3
$P -m ctm.cli select-board            # list known ESP32 USB bridges, pick one (y/N even if only one)
$P -m ctm.cli select-board --all      # every serial port
$P -m ctm.cli select-board --serial N # non-interactive
$P -m ctm.cli port                    # /dev path of the selected board (exit 1 if unplugged)
```

Listed: device, serial number, VID:PID and chip for WCH CH9102 (1A86:55D4), CH340 (1A86:7523),
CH343 (1A86:55D3), Silicon Labs CP210x (10C4:EA60), FTDI (0403:6001/6015) and Espressif
native USB (303A:*). Ports without a serial number are refused (they cannot be told apart
reliably), and so are two ports reporting the same number. A running bridge re-reads
`config.json` while it holds no port, so no restart is needed after `select-board`.
Matching is exact; a unique prefix match is accepted for serials recorded by hand.

Firmware upload to exactly that board (never rely on PlatformIO auto-detect with several boards attached):

```sh
~/.platformio/penv/bin/pio run -d firmware -e t-display -t upload \
    --upload-port "$(cd host && ~/.platformio/penv/bin/python3 -m ctm.cli port)"
```

## WiFi setup (pairing)

`ctm setup` is interactive (it asks for the WiFi password with a hidden prompt), so **you run it yourself** with the board plugged in by USB, **in a normal terminal (Terminal/iTerm)**. Claude Code's `!` prefix has no interactive stdin, so setup refuses to run there (exit 2):

```sh
cd host && ~/.platformio/penv/bin/python3 -m ctm.cli setup
```

What it does:
1. If the bridge (or `tools/serial_hold.py`) holds the port, it sends `SIGUSR1`, waits for `hold.state` = `PAUSED` (5s), and always resumes it at the end (also on Ctrl-C / errors).
2. Opens the board found by USB serial number (`--port-serial N` > `CTM_USB_SERIAL` > `config.json`). If no board is selected yet, setup first runs the `select-board` flow. Opening may reboot the board; it waits up to 6s for `hello`.
3. Scans and shows a numbered list (signal bars, lock = password needed). Pick a number, `m` to type a hidden SSID, `r` to rescan, `q` to quit.
4. Generates a fresh 32-byte key, sends `provision`, waits up to 25s. On success the key goes to the Keychain (`ctm-pairing` / `ctmeter-xxxx`) and `~/.claude-token-meter/device.json` gets host + IP. On failure it prints the reason (`no_ap`, `auth`, `timeout`, `bad_args`) and what to try.

The password and key are never printed or logged; the provision line bypasses the bridge log because the bridge is paused. `--dry-run` shows what would be sent (secrets masked) without opening anything.

```sh
python3 -m ctm.cli setup --dry-run
python3 -m ctm.cli info            # USB {"t":"info"} if plugged in, else GET http://<host>.local/v1/info
python3 -m ctm.cli info --wifi     # force WiFi
python3 -m ctm.cli unpair          # USB: device clears WiFi + key, then Keychain item + device.json are removed
python3 -m ctm.cli unpair --local  # forget the pairing on this Mac only
```

`setup`, `unpair` and USB `info` need pyserial (`~/.platformio/penv/bin/python3`).

## Run the bridge

`tools/serial_hold.py` must not be running, because both use the same `hold.pid`. The bridge refuses to start if `hold.pid` names a live process; `--force` overrides this.

```sh
cd host
~/.platformio/penv/bin/python3 -m ctm.bridge            # runs until SIGTERM / Ctrl-C (--transport auto)
~/.platformio/penv/bin/python3 -m ctm.bridge --transport wifi   # or usb
~/.platformio/penv/bin/python3 -m ctm.bridge --once     # print the frame it would send, no serial
kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid) # release port (upload), again to re-acquire
```

- The device is found by its USB serial number (`--usb-serial` > `CTM_USB_SERIAL` > `config.json`, see *Select your board*; none = WiFi only). The port is opened with DTR/RTS low and then locked with `TIOCEXCL`.
- State is in `serial/hold.state`. The log is `serial/YYYYMMDD.log`: RX lines are written as-is, TX lines start with `>> `, and the bridge's own events start with `[bridge]`.
- The bridge polls the sessions every 2s. It sends a frame when the merged windows change (at most once every 2s, `MIN_INTERVAL_S`), every 120s as a heartbeat, on connect, and right after the device sends `@{"t":"hello"}`.
- `seq = max(last_seq + 1, int(time.time()))` (uint32), so it never goes backwards across bridge restarts.
- Transport `auto` (default): USB while the target USB device is present (held exactly as above), otherwise WiFi if paired (`device.json` + Keychain key), otherwise wait. `usb` / `wifi` force one.
- WiFi: `POST http://<host>.local/v1/state` (getaddrinfo, falling back to the cached IP; the IP cache is updated on success) with `X-CTM-Sig: hmac_sha256(key, body)`. Change sends at most every 30s, heartbeat 120s, 3s timeouts, exponential backoff 5s → 10 min on failure. 401 logs "pairing key mismatch — rerun ctm setup"; 409 `old_seq` resyncs from `/v1/info` and retries once; 409 `skew` means the Mac clock is off; EHOSTUNREACH logs a macOS Local Network permission hint. `hold.state` shows `WIFI <host> <ip> ok seq=N` or `... backoff Ns (...)`.

## Robustness (P5)

- Sleep/wake & clock jumps: wall vs monotonic divergence > 30s, or > 30s between loop iterations, logs `[bridge] wake/clock jump detected`, resends at once, resets the WiFi backoff and distrusts the cached IP.
- IP change: after a failed POST (timeout, refused, unreachable) the cached IP is not trusted; each retry re-resolves `<host>.local` (the stale IP is only probed every 3rd attempt while mDNS fails). The new IP is written to `device.json` on success.
- USB boot race: after (re)acquiring the port the first frame waits for `@{"t":"hello"}` or 2.5s.
- Logs: `serial/YYYYMMDD.log` older than 14 days are deleted; past ~5 MB a day keeps only `[bridge]` events and `@` protocol / `<< wifi ack` lines (past 10 MB only events). WiFi acks are logged as `<< wifi ack seq=N ip=X` (used by `doctor`).

## Install (P6)

```sh
cd host
P=~/.platformio/penv/bin/python3
$P -m ctm.cli install --statusline --dry-run   # show the plist, launchctl commands, settings change
$P -m ctm.cli install --statusline             # [--replace] stop a hand-started bridge; [--force] replace another statusLine
$P -m ctm.cli doctor
$P -m ctm.cli uninstall [--statusline] [--dry-run]
```

The plist runs `<python> -u -m ctm.bridge --transport auto` in this directory (RunAtLoad, KeepAlive, ThrottleInterval 10, output in `~/.claude-token-meter/launchd/`). `--python` picks another interpreter (it must import `serial`).

## CLI

```sh
cd host
python3 -m ctm.cli status          # + transport, board (serial, source, /dev path), pairing, key present (never printed)
python3 -m ctm.cli push-test --pct5 61 --pct7 33   # fake session test-<ts>.json, resets in the future
python3 -m ctm.cli push-test --clear               # ALWAYS run afterwards
```

A test file has the latest `resets_at`, so it overrides the real sessions until you clear it.

## Tests

pytest is not installed, so the tests use stdlib `unittest`. They are also pytest-compatible.

```sh
cd host && /usr/bin/python3 -m unittest discover -s tests -t .
```

Shared fixtures are in `../fixtures/`:
- `merge_*.json`: `{now, sessions, expected}`
- `frame_*.json`: `{line, last_seq, expected: {result: ack|nak, err?, windows?}}`. Firmware native tests should reach the same verdicts.
- `hmac_*.json`: `{key_hex, body, sig, expected: ok|sig}` (§4.4), checked by `tests/test_wifi.py`.

Tests never open or enumerate a serial port (fake port lists), never touch the real Keychain (`tests/_util.py` makes `keystore.default()` raise) and use temp `CTM_HOME` directories. Install/doctor tests use temp LaunchAgents / settings.json paths and a fake `launchctl`.
