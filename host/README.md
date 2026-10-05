# host — ctm (P1, USB serial)

Implements `docs/PROTOCOL.md` v1. Files are under `~/.claude-token-meter/` (override with `CTM_HOME`).

| file | role |
|---|---|
| `ctm/statusline.py` | Claude Code statusLine command. Writes `sessions/<session_id>.json` (§1). Self-contained, stdlib only. |
| `ctm/merge.py` | Merge rules (§2) and pruning of session files older than 8 days |
| `ctm/proto.py` | Builds state frames (§3) and parses `@` lines. Also holds the reference device acceptance rules used by the fixtures. |
| `ctm/bridge.py` | Long-running bridge. Owns the serial port and sends frames. |
| `ctm/cli.py` | `status`, `push-test` |

## Register the statusLine

In `~/.claude/settings.json`:

```json
"statusLine": {"type": "command",
               "command": "/usr/bin/python3 -S /Users/byunginkim/dev/esp32/claude-token-meter/host/ctm/statusline.py"}
```

The script prints `5h 52% · 7d 29%`. It prints `--` when a value is unknown and `0%` once that window's `resets_at` has passed.

## Run the bridge

`tools/serial_hold.py` must not be running, because both use the same `hold.pid`. The bridge refuses to start if `hold.pid` names a live process; `--force` overrides this.

```sh
cd host
~/.platformio/penv/bin/python3 -m ctm.bridge            # runs until SIGTERM / Ctrl-C
~/.platformio/penv/bin/python3 -m ctm.bridge --once     # print the frame it would send, no serial
kill -USR1 $(cat ~/.claude-token-meter/serial/hold.pid) # release port (upload), again to re-acquire
```

- The device is found by its USB serial number, `CTM_USB_SERIAL` (default `5915018996`). The port is opened with DTR/RTS low and then locked with `TIOCEXCL`.
- State is in `serial/hold.state`. The log is `serial/YYYYMMDD.log`: RX lines are written as-is, TX lines start with `>> `, and the bridge's own events start with `[bridge]`.
- The bridge polls the sessions every 2s. It sends a frame when the merged windows change (at most once every 2s, `MIN_INTERVAL_S`), every 120s as a heartbeat, on connect, and right after the device sends `@{"t":"hello"}`.

## CLI

```sh
cd host
python3 -m ctm.cli status
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
