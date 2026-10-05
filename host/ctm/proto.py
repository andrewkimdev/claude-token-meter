"""PROTOCOL v1 §3/§4.1: state frame building and device line parsing.

Also contains `device_accept`, a Python reference of the device-side
acceptance rules, used to check fixtures/frame_*.json (firmware native tests
should reach the same verdicts).
"""
import json

VERSION = 1
MAX_LINE = 1024


def build_frame(seq, sent_at, windows):
    """Return the frame dict (§3). windows comes from merge.merge()."""
    return {
        "t": "state",
        "v": VERSION,
        "seq": int(seq),
        "sent_at": int(sent_at),
        "windows": windows,
    }


def encode_frame(frame):
    """Compact one-line JSON terminated by \\n, as bytes."""
    b = (json.dumps(frame, separators=(",", ":"), sort_keys=False) + "\n").encode("utf-8")
    if len(b) > MAX_LINE:
        raise ValueError("frame exceeds %d bytes" % MAX_LINE)
    return b


def parse_device_line(line):
    """Device -> host line. Returns the dict for '@{...}' protocol lines,
    None for free-form log text or unparsable '@' lines.

    The '@{' may be preceded by junk on the same line: after a reset the ROM
    boot banner (74880bps, garbled at 115200) has no newline before the first
    app line, so `hello` arrives glued to it [V, P1]."""
    i = line.rfind("@{")
    if i < 0:
        return None
    try:
        d = json.loads(line[i + 1:])
    except ValueError:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("t"), str) else None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def device_accept(line, last_seq):
    """Reference of the device rules. last_seq None = fresh boot.
    Returns {"result": "ack"|"nak", "err"?: "parse|old_seq|bad_v",
             "windows"?: {key: {"pct","resets_at"}}}."""
    try:
        if len(line.encode("utf-8")) > MAX_LINE:
            raise ValueError("too long")
        f = json.loads(line)
        if not isinstance(f, dict) or f.get("t") != "state":
            raise ValueError("not state")
    except ValueError:
        return {"result": "nak", "err": "parse"}
    if f.get("v") != VERSION:
        return {"result": "nak", "err": "bad_v"}
    seq = f.get("seq")
    windows = f.get("windows")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and _num(f.get("sent_at"))
            and isinstance(windows, dict)):
        return {"result": "nak", "err": "parse"}
    # seq <= last_seq is old, except a bridge restart (seq == 1 < last_seq).
    if last_seq is not None and seq <= last_seq and not (seq == 1 and seq < last_seq):
        return {"result": "nak", "err": "old_seq"}
    out = {}
    for k in ("five_hour", "seven_day"):  # unknown keys ignored
        w = windows.get(k)
        if w is None:
            continue
        if not (isinstance(w, dict) and _num(w.get("pct")) and _num(w.get("resets_at"))):
            return {"result": "nak", "err": "parse"}
        out[k] = {"pct": w["pct"], "resets_at": w["resets_at"]}
    return {"result": "ack", "windows": out}
