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


def _epoch(v):
    return _num(v) and v >= 0 and v == v and v not in (float("inf"),)


def device_accept(line, last_seq):
    """Reference of the device rules (firmware/src/core/protocol.cpp is the
    authority; PROTOCOL.md §3.1). last_seq None = fresh boot.
    Returns {"result": "ack"|"nak"|"ignored", "err"?: "parse|old_seq|bad_v",
             "windows"?: {key: {"pct","resets_at"}}}."""
    try:
        if len(line.encode("utf-8")) > MAX_LINE:
            raise ValueError("too long")
        f = json.loads(line)
    except ValueError:
        return {"result": "nak", "err": "parse"}
    if not isinstance(f, dict) or not isinstance(f.get("t"), str):
        return {"result": "nak", "err": "parse"}
    if f["t"] != "state":
        return {"result": "ignored"}          # other message types: no reply
    v = f.get("v")
    if not (isinstance(v, int) and not isinstance(v, bool)) or v != VERSION:
        return {"result": "nak", "err": "bad_v"}
    seq = f.get("seq")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and 1 <= seq <= 0xFFFFFFFF):
        return {"result": "nak", "err": "parse"}
    if not _epoch(f.get("sent_at")):
        return {"result": "nak", "err": "parse"}
    windows = f.get("windows")
    if windows is None:
        windows = {}                          # windows optional -> empty
    if not isinstance(windows, dict):
        return {"result": "nak", "err": "parse"}
    out = {}
    for k in ("five_hour", "seven_day"):  # unknown keys ignored
        w = windows.get(k)
        if w is None:
            continue
        if not (isinstance(w, dict) and _num(w.get("pct")) and _epoch(w.get("resets_at"))):
            return {"result": "nak", "err": "parse"}
        if w.get("observed_at") is not None and not _epoch(w["observed_at"]):
            return {"result": "nak", "err": "parse"}
        out[k] = {"pct": min(100, max(0, w["pct"])), "resets_at": w["resets_at"]}
    # seq gate last: a malformed frame never advances it.
    # seq <= last_seq is old, except a bridge restart (seq == 1 < last_seq).
    if last_seq is not None and seq <= last_seq and not (seq == 1 and seq < last_seq):
        return {"result": "nak", "err": "old_seq"}
    return {"result": "ack", "windows": out}
