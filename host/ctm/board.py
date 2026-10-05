"""Which USB board is "ours": <CTM_HOME>/config.json {"usb_serial": "..."}.

Resolution order (everywhere: bridge, setup/unpair/info, status, doctor,
port, tools/serial_hold.py):

    explicit CLI flag  >  env CTM_USB_SERIAL  >  config.json "usb_serial"  >  None

There is NO built-in default. With None the USB transport is simply off:
nothing ever scans for "some ESP32" and opens it, because a Mac with several
dev boards attached would get the wrong one (and a port open resets it).

`ctm select-board` lists candidate ports and writes config.json.
Ports are matched by USB serial number (exact; a unique prefix match is
accepted for serials recorded before select-board existed). Two ports with
the same number are an error, never a guess.

Python 3.9 compatible; pyserial is imported only when ports are listed.
"""
import json
import os
import sys

ENV_VAR = "CTM_USB_SERIAL"
CONFIG_NAME = "config.json"
KEY = "usb_serial"

# Known USB-UART bridges / native USB on ESP32 dev boards, (vid, pid) -> name.
# pid None = any pid of that vendor.
KNOWN_CHIPS = {
    (0x1A86, 0x55D4): "WCH CH9102",
    (0x1A86, 0x7523): "WCH CH340",
    (0x1A86, 0x55D3): "WCH CH343",
    (0x10C4, 0xEA60): "Silicon Labs CP210x",
    (0x0403, 0x6001): "FTDI FT232R",
    (0x0403, 0x6015): "FTDI FT231X",
    (0x303A, None): "Espressif native USB",
}

NO_BOARD_HINT = "no board selected \u2014 run `ctm select-board`"


class BoardError(Exception):
    pass


def ctm_home():
    return os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")


# ---- config.json -------------------------------------------------------------

def config_path(home=None):
    return os.path.join(home or ctm_home(), CONFIG_NAME)


def load_config(home=None):
    """dict (empty if absent / unreadable / not an object)."""
    try:
        with open(config_path(home)) as f:
            d = json.load(f)
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def save_config(home, **updates):
    """Merge updates into config.json (atomic). Returns the new dict."""
    home = home or ctm_home()
    d = load_config(home)
    d.update(updates)
    os.makedirs(home, exist_ok=True)
    p = config_path(home)
    tmp = p + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, p)
    return d


def _clean(v):
    if v is None:
        return None
    v = str(v).strip()
    return v or None


def resolve_serial(flag=None, home=None, environ=None):
    """(serial or None, source) with source in flag|env|config|none."""
    environ = os.environ if environ is None else environ
    v = _clean(flag)
    if v:
        return v, "flag"
    v = _clean(environ.get(ENV_VAR))
    if v:
        return v, "env"
    v = _clean(load_config(home).get(KEY))
    if v:
        return v, "config"
    return None, "none"


# ---- port listing / matching -------------------------------------------------

def _comports():
    from serial.tools import list_ports
    return list_ports.comports()


def chip_name(vid, pid):
    if vid is None:
        return None
    return KNOWN_CHIPS.get((vid, pid)) or KNOWN_CHIPS.get((vid, None))


def describe(p):
    """pyserial ListPortInfo (or a fake with the same attributes) -> dict."""
    vid, pid = getattr(p, "vid", None), getattr(p, "pid", None)
    return {
        "device": p.device,
        "serial": _clean(getattr(p, "serial_number", None)),
        "vid": vid,
        "pid": pid,
        "vidpid": ("%04X:%04X" % (vid, pid)) if vid is not None and pid is not None else "-",
        "chip": chip_name(vid, pid),
        "desc": getattr(p, "description", None) or "",
    }


def list_ports(show_all=False, comports=None):
    """Callout ports (macOS /dev/tty.* twins are skipped). Known ESP32 bridges
    only unless show_all."""
    ports = (comports or _comports)()
    out = []
    for p in ports:
        d = describe(p)
        if d["device"].startswith("/dev/tty."):
            continue
        if show_all or d["chip"]:
            out.append(d)
    out.sort(key=lambda d: d["device"])
    return out


def matches(serial_no, comports=None):
    """Callout devices whose USB serial number is serial_no (exact), else
    those whose serial starts with it (legacy). [] when serial_no is None."""
    serial_no = _clean(serial_no)
    if not serial_no:
        return []
    ports = [describe(p) for p in (comports or _comports)()]
    ports = [d for d in ports if d["serial"] and "cu." in d["device"]]
    exact = [d["device"] for d in ports if d["serial"] == serial_no]
    if exact:
        return sorted(exact)
    return sorted(d["device"] for d in ports if d["serial"].startswith(serial_no))


def find_port(serial_no, comports=None):
    """Device path of the board with this serial, None if absent or no serial
    is configured. Raises BoardError if several ports match (never guesses)."""
    m = matches(serial_no, comports)
    if len(m) > 1:
        raise BoardError("%d ports match USB serial %s (%s); refusing to guess"
                         % (len(m), serial_no, ", ".join(m)))
    return m[0] if m else None


# ---- `ctm select-board` --------------------------------------------------------

def _row(i, d, current):
    mark = "  (current)" if current and d["serial"] == current else ""
    return "  %2d) %-34s serial %-20s %s  %s%s" % (
        i, d["device"], d["serial"] or "(none)", d["vidpid"],
        d["chip"] or d["desc"] or "?", mark)


NO_SERIAL_WHY = ("%s reports no USB serial number, so it cannot be told apart from "
                 "other boards reliably (its /dev name can change between plugs). "
                 "Use a board/cable whose USB bridge reports a serial number.")


def _save(home, serial, out, environ=None):
    environ = os.environ if environ is None else environ
    save_config(home, **{KEY: serial})
    out.write("Saved usb_serial=%s to %s\n" % (serial, config_path(home)))
    if _clean(environ.get(ENV_VAR)) and _clean(environ.get(ENV_VAR)) != serial:
        out.write("note: %s=%s is set and overrides config.json in this shell.\n"
                  % (ENV_VAR, environ.get(ENV_VAR)))
    out.write("A running bridge picks the new board up within a few seconds.\n")


def select_board(home=None, serial=None, show_all=False, input_fn=input, out=sys.stdout,
                 comports=None, environ=None):
    """Interactive (serial None) or non-interactive (serial given) selection.
    Returns (exit code, chosen serial or None)."""
    home = home or ctm_home()
    if serial is not None:
        serial = _clean(serial)
        if not serial:
            out.write("--serial must not be empty\n")
            return 2, None
        try:
            dev = find_port(serial, comports)
        except BoardError as e:
            out.write("ctm select-board: %s\n" % e)
            return 2, None
        except ImportError:
            dev = None
        _save(home, serial, out, environ=environ)
        out.write("board %s: %s\n" % (serial, dev or "not plugged in right now"))
        return 0, serial

    try:
        ports = list_ports(show_all, comports)
    except ImportError:
        out.write("pyserial is not installed for this python; use "
                  "~/.platformio/penv/bin/python3\n")
        return 2, None
    current = _clean(load_config(home).get(KEY))
    if current:
        out.write("Current board (config.json): usb_serial %s\n" % current)
    if not ports:
        out.write("No %sUSB serial ports found. Plug the board in (data cable) and retry%s.\n"
                  % ("" if show_all else "ESP32-style ", "" if show_all else ", or use --all"))
        return 1, None
    out.write("%s:\n" % ("All serial ports" if show_all else
                        "Candidate ESP32 boards (known USB bridges; --all shows every port)"))
    for i, d in enumerate(ports, 1):
        out.write(_row(i, d, current) + "\n")

    if len(ports) == 1:
        d = ports[0]
        if not d["serial"]:
            out.write("Refused: " + NO_SERIAL_WHY % d["device"] + "\n")
            return 2, None
        ans = input_fn("Use %s as the meter board? [y/N]: " % d["device"]).strip().lower()
        if not ans.startswith("y"):
            out.write("Nothing changed.\n")
            return 1, None
    else:
        while True:
            ans = input_fn("Select the meter board [1-%d], q = quit: " % len(ports)).strip().lower()
            if ans == "q" or ans == "":
                out.write("Nothing changed.\n")
                return 1, None
            if ans.isdigit() and 1 <= int(ans) <= len(ports):
                d = ports[int(ans) - 1]
                break
            out.write("Invalid choice.\n")
    if not d["serial"]:
        out.write("Refused: " + NO_SERIAL_WHY % d["device"] + "\n")
        return 2, None
    same = [x["device"] for x in list_ports(True, comports) if x["serial"] == d["serial"]]
    if len(same) > 1:
        out.write("Refused: %d ports report the same serial %s (%s); they cannot be told "
                  "apart. Unplug the other board(s) or use a different one.\n"
                  % (len(same), d["serial"], ", ".join(same)))
        return 2, None
    _save(home, d["serial"], out, environ=environ)
    return 0, d["serial"]
