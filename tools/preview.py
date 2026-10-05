#!/usr/bin/env python3
"""Render the device's 240x135 screen to PNG without a device (P3).

Mirrors firmware/src/ui/render.cpp (coordinates, RGB565 colours, rules) and
firmware/src/core/{meter,view_state}.cpp. Keep them in sync by hand: when the
firmware layout changes, change this file in the same commit.

Glyphs are pixel-exact: the TFT_eSPI built-in fonts (1, 2, 4, 7) are parsed
from the library sources that PlatformIO downloaded into
firmware/.pio/libdeps/t-display/TFT_eSPI/Fonts (run a firmware build first).
Approximate parts: rounded rectangles (toast) use Pillow's primitive, and
RGB565 -> RGB888 is a plain bit expansion (panel gamma not modelled).

Needs Pillow. The PlatformIO venv has it:
  ~/.platformio/penv/bin/python3 tools/preview.py --samples
  ~/.platformio/penv/bin/python3 tools/preview.py frame.json --now 1791180000 -o out.png
  ... --view detail --frame-age 200 --uptime 3600 --heap 150000 --toast "run ctm setup"
  ... (no frame argument) -> waiting screen
  ... --via wifi --provisioned --wifi connected --ip 192.168.1.23 --rssi -61
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    from PIL import Image, ImageDraw
except ImportError:  # fail closed with a usable hint
    sys.exit("Pillow missing. Use ~/.platformio/penv/bin/python3 tools/preview.py")

ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = ROOT / "firmware/.pio/libdeps/t-display/TFT_eSPI/Fonts"
OUT_DIR = ROOT / "tools/preview_out"

FW_VERSION = "0.2.0"          # core/protocol.h
TZ_OFFSET_S = 9 * 3600        # firmware: setenv("TZ","KST-9") == UTC+9

# ---------------------------------------------------------------- fonts ----


def _preprocess(src: str) -> str:
    """Tiny C preprocessor: #define NAME, #ifdef/#ifndef/#else/#endif."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    defined: set[str] = set()
    stack: list[bool] = []
    out = []
    for line in src.splitlines():
        line = line.split("//", 1)[0]
        s = line.strip()
        active = all(stack)
        if s.startswith("#ifdef"):
            stack.append(s.split()[1] in defined)
        elif s.startswith("#ifndef"):
            stack.append(s.split()[1] not in defined)
        elif s.startswith("#else"):
            stack[-1] = not stack[-1]
        elif s.startswith("#endif"):
            stack.pop()
        elif s.startswith("#define"):
            if active:
                defined.add(s.split()[1])
        elif active:
            out.append(line)
    return "\n".join(out)


def _arrays(path: Path) -> dict[str, list[int]]:
    text = _preprocess(path.read_text())
    arrs = {}
    for m in re.finditer(r"(\w+)\s*\[\s*\w*\s*\]\s*(?:PROGMEM\s*)?=\s*\{(.*?)\}\s*;", text, re.S):
        vals = [int(v, 0) for v in re.findall(r"0[xX][0-9A-Fa-f]+|\d+", m.group(2))]
        arrs[m.group(1)] = vals
    return arrs


@dataclass
class Font:
    height: int
    widths: list[int]                       # index = char - 32
    glyphs: dict[int, list[tuple[int, int]]] = field(default_factory=dict)  # set pixels


def _load_bitmap_font2() -> Font:
    a = _arrays(FONT_DIR / "Font16.c")
    f = Font(16, a["widtbl_f16"])
    for c in range(32, 128):
        data = a.get(f"chr_f16_{c:02X}")
        if data is None:
            continue
        w = f.widths[c - 32]
        bpr = (w + 6) // 8
        px = []
        for row in range(16):
            for k in range(bpr):
                b = data[row * bpr + k]
                for bit in range(8):
                    if b & (0x80 >> bit):
                        px.append((k * 8 + bit, row))
        f.glyphs[c] = px
    return f


def _load_rle_font(fname: str, tag: str, height: int) -> Font:
    a = _arrays(FONT_DIR / fname)
    f = Font(height, a[f"widtbl_{tag}"])
    for c in range(32, 128):
        data = a.get(f"chr_{tag}_{c:02X}")
        if data is None:
            continue
        w = f.widths[c - 32]
        total, pc, px = w * height, 0, []
        for b in data:
            if pc >= total:
                break
            n = (b & 0x7F) + 1
            if b & 0x80:
                px.extend(((pc + i) % w, (pc + i) // w) for i in range(n))
            pc += n
        f.glyphs[c] = px
    return f


def _load_glcd() -> Font:
    a = _arrays(FONT_DIR / "glcdfont.c")["font"]
    f = Font(8, [6] * 96)
    for c in range(32, 128):
        cc = c + 1 if c > 175 else c   # !_cp437 quirk; irrelevant for ASCII
        f.glyphs[c] = [(i, j) for i in range(5) for j in range(8)
                       if a[cc * 5 + i] & (1 << j)]
    return f


FONTS: dict[int, Font] = {}


def load_fonts() -> None:
    if not FONT_DIR.is_dir():
        sys.exit(f"TFT_eSPI fonts not found at {FONT_DIR}\n"
                 "Run `~/.platformio/penv/bin/pio run -d firmware -e t-display` once.")
    FONTS[1] = _load_glcd()
    FONTS[2] = _load_bitmap_font2()
    FONTS[4] = _load_rle_font("Font32rle.c", "f32", 26)
    FONTS[7] = _load_rle_font("Font7srle.c", "f7s", 48)

# --------------------------------------------------------------- canvas ----

TL, TR, MC = "TL", "TR", "MC"


def rgb(c565: int) -> tuple[int, int, int]:
    r, g, b = (c565 >> 11) & 31, (c565 >> 5) & 63, c565 & 31
    return (r << 3 | r >> 2, g << 2 | g >> 4, b << 3 | b >> 2)


class Sprite:
    """Subset of TFT_eSprite used by render.cpp. Text is transparent (1-arg
    setTextColor), as on the device."""

    def __init__(self, w=240, h=135):
        self.img = Image.new("RGB", (w, h), (0, 0, 0))
        self.d = ImageDraw.Draw(self.img)
        self.datum = TL
        self.color = 0xFFFF

    def fill_rect(self, x, y, w, h, c):
        if w > 0 and h > 0:
            self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=rgb(c))

    def hline(self, x, y, w, c):
        self.fill_rect(x, y, w, 1, c)

    def fill_circle(self, x0, y0, r, c):   # port of TFT_eSPI::fillCircle
        x, dx, dy, p = 0, 1, r + r, -(r >> 1)
        self.hline(x0 - r, y0, dy + 1, c)
        while x < r:
            if p >= 0:
                self.hline(x0 - x, y0 + r, dx, c)
                self.hline(x0 - x, y0 - r, dx, c)
                dy -= 2
                p -= dy
                r -= 1
            dx += 2
            p += dx
            x += 1
            self.hline(x0 - r, y0 + x, dy + 1, c)
            self.hline(x0 - r, y0 - x, dy + 1, c)

    def round_rect(self, x, y, w, h, r, c, fill):   # approximate
        box = [x, y, x + w - 1, y + h - 1]
        if fill:
            self.d.rounded_rectangle(box, r, fill=rgb(c))
        else:
            self.d.rounded_rectangle(box, r, outline=rgb(c))

    def text_width(self, s, font):
        f = FONTS[font]
        return sum(f.widths[ord(ch) - 32] if 32 <= ord(ch) < 128 else f.widths[0] for ch in s)

    def draw_string(self, s, x, y, font):
        f = FONTS[font]
        w = self.text_width(s, font)
        if self.datum == TR:
            x -= w
        elif self.datum == MC:
            x -= w // 2
            y -= f.height // 2
        col = rgb(self.color)
        sum_x = 0
        for ch in s:
            c = ord(ch)
            if not 32 <= c < 128:
                continue
            for px, py in f.glyphs.get(c, ()):
                X, Y = x + sum_x + px, y + py
                if 0 <= X < self.img.width and 0 <= Y < self.img.height:
                    self.img.putpixel((X, Y), col)
            sum_x += f.widths[c - 32]
        return sum_x

# ---------------------------------------------------- core (meter.cpp) ----

FIVE_HOUR_S, SEVEN_DAY_S = 5 * 3600, 7 * 86400
LINK_FRESH_S, LINK_OFFLINE_S = 150, 300


@dataclass
class Window:
    present: bool = False
    pct: float = 0.0
    resets_at: int = 0
    observed_at: int = 0


@dataclass
class Frame:
    seq: int = 0
    sent_at: int = 0
    five_hour: Window = field(default_factory=Window)
    seven_day: Window = field(default_factory=Window)


def parse_frame(d: dict) -> Frame:
    """Lenient mirror of protocol.cpp (validation is the firmware's job)."""
    def win(w):
        if not w:
            return Window()
        pct = min(100.0, max(0.0, float(w["pct"])))
        return Window(True, pct, int(w["resets_at"]), int(w.get("observed_at", d["sent_at"])))
    ws = d.get("windows") or {}
    return Frame(int(d.get("seq", 0)), int(d["sent_at"]), win(ws.get("five_hour")), win(ws.get("seven_day")))


def lround(x):
    return int(math.floor(x + 0.5)) if x >= 0 else -int(math.floor(-x + 0.5))


@dataclass
class View:
    state: str = "missing"   # missing | reset | live
    pct_int: int = 0
    pct: float = 0
    pace: float = 0
    remaining: int = 0
    level: str = "neutral"   # neutral | amber | orange | red


def level_for(pct, pace):
    if pct >= 95: return "red"
    if pct >= 80: return "orange"
    if pct > pace * 100: return "amber"
    return "neutral"


def make_view(w: Window, now, wlen) -> View:
    v = View()
    if not w.present:
        return v
    if w.resets_at <= now:
        v.state = "reset"
        return v
    v.state, v.pct, v.pct_int = "live", w.pct, lround(w.pct)
    v.pace = min(1.0, max(0.0, 1 - (w.resets_at - now) / wlen))
    v.remaining = w.resets_at - now
    v.level = level_for(w.pct, v.pace)
    return v


def fmt_countdown(s):
    s = max(0, int(s))
    if s < 60: return "<1m"
    if s < 3600: return f"{s // 60}m"
    if s < 86400: return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"


def fmt_age(s):
    s = max(0, int(s))
    if s < 60: return f"{s}s ago"
    if s < 3600: return f"{s // 60}m ago"
    if s < 86400: return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def latest_observed(f: Frame):
    return max([w.observed_at for w in (f.five_hour, f.seven_day) if w.present] or [0])


def link_state(age):
    if age <= LINK_FRESH_S: return "fresh"
    if age <= LINK_OFFLINE_S: return "stale"
    return "offline"

# ---------------------------------------------------- render.cpp mirror ----

C_TEXT, C_NEUTRAL, C_DIM, C_TRACK = 0xFFFF, 0xBDF7, 0x7BEF, 0x2945
C_AMBER, C_ORANGE, C_RED, C_TICK, C_GREEN, C_BLACK = 0xFE60, 0xFC00, 0xF800, 0x07FF, 0x07E0, 0
PAD, W, H = 4, 240, 135
RIGHT, BAR_X, BAR_W = W - PAD, PAD, W - 2 * PAD
Y5_PCT, Y5_LBL, Y5_CD, Y5_BAR, H5_BAR = 4, 4, 30, 58, 10
Y7_PCT, Y7_BAR, H7_BAR = 77, 106, 6
Y_FOOT = 119
DET_Y0, DET_DY, DET_VX = 2, 16, 76


@dataclass
class Model:
    have_frame: bool = False
    frame: Frame = field(default_factory=Frame)
    now: int = 0
    view: str = "main"
    frame_age: int = 0
    last_seq: int = 0
    uptime: int = 0
    free_heap: int = 0
    toast: str | None = None
    # P4
    via_wifi: bool = False          # transport of the last accepted frame
    provisioned: bool = False
    wifi: str = "off"               # off | connecting | connected
    host: str = "ctmeter-a1b2"
    ip: str = ""                    # "" unless connected
    rssi: int = 0


def level_color(l):
    return {"red": C_RED, "orange": C_ORANGE, "amber": C_AMBER}.get(l, C_TEXT)


def bar_color(l):
    return C_NEUTRAL if l == "neutral" else level_color(l)


def draw_bar(s: Sprite, y, h, v: View):
    s.fill_rect(BAR_X, y, BAR_W, h, C_TRACK)
    if v.state != "live":
        return
    fw = int(BAR_W * v.pct / 100.0 + 0.5)
    if fw > 0:
        s.fill_rect(BAR_X, y, fw, h, bar_color(v.level))
    tx = min(BAR_X + int(BAR_W * v.pace + 0.5), BAR_X + BAR_W - 2)
    s.fill_rect(tx, y - 3, 2, h + 6, C_TICK)


def draw_countdown(s: Sprite, y, v: View):
    s.datum = TR
    if v.state == "live":
        s.color = C_TEXT
        s.draw_string(fmt_countdown(v.remaining), RIGHT, y, 4)
    elif v.state == "reset":
        s.color = C_DIM
        s.draw_string("reset", RIGHT, y, 4)
    s.datum = TL


def draw_five_hour(s: Sprite, v: View):
    s.datum, s.color = TR, C_DIM
    s.draw_string("5H", RIGHT, Y5_LBL, 4)
    s.datum = TL
    draw_countdown(s, Y5_CD, v)
    col = C_DIM
    if v.state == "live":
        col, num = level_color(v.level), str(v.pct_int)
    else:
        num = "0" if v.state == "reset" else "--"
    s.color = col
    w = s.draw_string(num, PAD, Y5_PCT, 7)
    if v.state != "missing":
        s.draw_string("%", PAD + w + 4, Y5_PCT + 48 - 26, 4)
    draw_bar(s, Y5_BAR, H5_BAR, v)


def draw_seven_day(s: Sprite, v: View):
    col = C_DIM
    if v.state == "live":
        col, num = level_color(v.level), f"{v.pct_int}%"
    else:
        num = "0%" if v.state == "reset" else "--"
    s.color = col
    w = s.draw_string(num, PAD, Y7_PCT, 4)
    s.color = C_DIM
    s.draw_string("7D", PAD + w + 8, Y7_PCT + 6, 2)
    draw_countdown(s, Y7_PCT, v)
    draw_bar(s, Y7_BAR, H7_BAR, v)


def draw_footer(s: Sprite, m: Model):
    link = link_state(m.frame_age)
    dot, txt, label = C_GREEN, C_TEXT, "WiFi" if m.via_wifi else "USB"
    if link == "stale":
        dot = txt = C_DIM
    elif link == "offline":
        dot, txt, label = C_RED, C_RED, "host offline"
    s.fill_circle(PAD + 4, Y_FOOT + 8, 4, dot)
    s.color = txt
    s.draw_string(label, PAD + 12, Y_FOOT, 2)
    obs = latest_observed(m.frame)
    s.datum, s.color = TR, C_DIM
    s.draw_string(fmt_age(m.now - obs) if obs > 0 else "no data", RIGHT, Y_FOOT, 2)
    s.datum = TL


def detail_row(s: Sprite, row, label, value, col):
    y = DET_Y0 + row * DET_DY
    s.color = C_DIM
    s.draw_string(label, PAD, y, 2)
    s.color = col
    s.draw_string(value, DET_VX, y, 2)


def reset_row(s: Sprite, row, label, w: Window, now):
    if not w.present:
        return detail_row(s, row, label, "--", C_DIM)
    lt = time.gmtime(w.resets_at + TZ_OFFSET_S)   # == localtime_r with TZ=KST-9
    if w.resets_at > now:
        detail_row(s, row, label, time.strftime("%a %m-%d %H:%M", lt), C_TEXT)
    else:
        detail_row(s, row, label, time.strftime("passed %a %H:%M", lt), C_DIM)


def draw_detail(s: Sprite, m: Model):
    reset_row(s, 0, "5H reset", m.frame.five_hour, m.now)
    reset_row(s, 1, "7D reset", m.frame.seven_day, m.now)
    via = "WiFi" if m.via_wifi else "USB"
    link = link_state(m.frame_age)
    lc = {"fresh": C_GREEN, "stale": C_DIM}.get(link, C_RED)
    if m.have_frame:
        detail_row(s, 2, "link", f"{via}, frame {fmt_age(m.frame_age)}", lc)
    else:
        detail_row(s, 2, "link", "no frame yet", C_DIM)
    if m.wifi == "connected":
        detail_row(s, 3, "wifi", f"connected, {m.rssi} dBm", C_GREEN)
    elif m.wifi == "connecting":
        detail_row(s, 3, "wifi", "connecting...", C_AMBER)
    else:
        detail_row(s, 3, "wifi", "off" if m.provisioned else "off (run ctm setup)", C_DIM)
    detail_row(s, 4, "ip", m.ip or "--", C_TEXT if m.ip else C_DIM)
    detail_row(s, 5, "host", f"{m.host}.local", C_TEXT if m.provisioned else C_DIM)
    detail_row(s, 6, "up / heap", f"{fmt_countdown(m.uptime)} / {m.free_heap} B", C_TEXT)
    y = DET_Y0 + 7 * DET_DY
    s.color = C_DIM
    s.draw_string(f"fw {FW_VERSION}  KST", PAD, y, 2)
    s.datum = TR
    s.draw_string("BTN1: back", RIGHT, y, 2)
    s.datum = TL


def draw_toast(s: Sprite, msg):
    tw, th = 160, 30
    x, y = (W - tw) // 2, (H - th) // 2
    s.round_rect(x, y, tw, th, 6, C_BLACK, True)
    s.round_rect(x, y, tw, th, 6, C_TEXT, False)
    s.datum, s.color = MC, C_TEXT
    s.draw_string(msg, W // 2, H // 2, 2)
    s.datum = TL


def draw_waiting(s: Sprite, m: Model):
    s.datum, s.color = MC, C_TEXT
    s.draw_string("Claude Token Meter", W // 2, 30, 4)
    s.color = C_DIM
    s.draw_string("waiting for host...", W // 2, 62, 2)
    if not m.provisioned:
        s.draw_string("USB serial 115200", W // 2, 80, 2)
        s.color = C_TEXT
        s.draw_string("USB: run ctm setup", W // 2, 98, 2)
    else:
        s.draw_string(f"WiFi: {m.host}.local", W // 2, 80, 2)
        if m.wifi == "connected":
            s.color = C_GREEN
            s.draw_string(f"connected {m.ip}", W // 2, 98, 2)
        else:
            s.color = C_AMBER
            s.draw_string("connecting...", W // 2, 98, 2)
    s.color = C_DIM
    s.draw_string(f"fw {FW_VERSION}", W // 2, 124, 1)
    s.datum = TL


def render(m: Model) -> Image.Image:
    s = Sprite()
    if m.view == "detail":
        draw_detail(s, m)
    elif not m.have_frame:
        draw_waiting(s, m)
    else:
        draw_five_hour(s, make_view(m.frame.five_hour, m.now, FIVE_HOUR_S))
        draw_seven_day(s, make_view(m.frame.seven_day, m.now, SEVEN_DAY_S))
        draw_footer(s, m)
    if m.toast:
        draw_toast(s, m.toast)
    return s.img

# ---------------------------------------------------------------- CLI ----


def save(img: Image.Image, path: Path, scale: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    if scale != 1:
        img = img.resize((img.width * scale, img.height * scale), Image.NEAREST)
    img.save(path)
    print(path)


def frame_dict(now, seq, five=None, seven=None):
    ws = {}
    if five: ws["five_hour"] = five
    if seven: ws["seven_day"] = seven
    return {"t": "state", "v": 1, "seq": seq, "sent_at": now, "windows": ws}


def win(pct, resets_in, now, observed_ago=0):
    return {"pct": pct, "resets_at": now + resets_in, "observed_at": now - observed_ago}


def samples(scale: int):
    now = 1791180000
    h, d = 3600, 86400
    std = dict(uptime=2 * d + 5 * h + 17 * 60, free_heap=154_320)
    wifi_ok = dict(provisioned=True, wifi="connected", ip="192.168.1.23", rssi=-61)

    def mdl(fd, **kw):
        f = parse_frame(fd)
        base = dict(have_frame=True, frame=f, now=now, last_seq=f.seq, frame_age=12, **std)
        base.update(kw)
        return Model(**base)

    cases = {
        "normal":        mdl(frame_dict(now, 41, win(30, 3 * h, now, 40), win(20, 4 * d, now, 40))),
        "amber_overpace": mdl(frame_dict(now, 42, win(62, 3 * h, now, 40), win(55, 4 * d, now, 40))),
        "orange_80":     mdl(frame_dict(now, 43, win(84, 2 * h, now, 40), win(45, 4 * d, now, 40))),
        "red_95":        mdl(frame_dict(now, 44, win(97, 47 * 60, now, 40), win(96, 18 * h, now, 40))),
        "reset_5h_passed": mdl(frame_dict(now, 45, win(71, -10 * 60, now, 2 * h), win(33, 4 * d, now, 2 * h))),
        "missing_window": mdl(frame_dict(now, 46, None, win(33, 4 * d, now, 40))),
        "stale_grey":    mdl(frame_dict(now, 47, win(30, 3 * h, now, 25 * 60), win(20, 4 * d, now, 25 * 60)),
                             frame_age=200),
        "offline_red":   mdl(frame_dict(now, 48, win(30, 3 * h, now, 52 * 60), win(20, 4 * d, now, 52 * 60)),
                             frame_age=9 * 60),
        "waiting":       Model(now=now, **std),   # unprovisioned
        "waiting_wifi_connecting": Model(now=now, provisioned=True, wifi="connecting", **std),
        "waiting_wifi_connected":  Model(now=now, **wifi_ok, **std),
        "wifi_footer":   mdl(frame_dict(now, 52, win(30, 3 * h, now, 40), win(20, 4 * d, now, 40)),
                             via_wifi=True, **wifi_ok),
        "wifi_stale":    mdl(frame_dict(now, 53, win(30, 3 * h, now, 25 * 60), win(20, 4 * d, now, 25 * 60)),
                             frame_age=200, via_wifi=True, **wifi_ok),
        "detail":        mdl(frame_dict(now, 49, win(62, 3 * h, now, 40), win(55, 4 * d, now, 40)),
                             view="detail", frame_age=34, via_wifi=True, **wifi_ok),
        "detail_unprovisioned": mdl(frame_dict(now, 54, win(62, 3 * h, now, 40), win(55, 4 * d, now, 40)),
                                    view="detail", frame_age=12),
        "detail_5h_passed": mdl(frame_dict(now, 50, win(71, -10 * 60, now, 2 * h), win(33, 4 * d, now, 2 * h)),
                                view="detail", frame_age=200, provisioned=True, wifi="connecting"),
        "toast_setup":   mdl(frame_dict(now, 51, win(30, 3 * h, now, 40), win(20, 4 * d, now, 40)),
                             toast="run ctm setup"),
    }
    for name, m in cases.items():
        save(render(m), OUT_DIR / f"{name}.png", scale)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("frame", nargs="?", help="state frame JSON file (PROTOCOL §3); omit for waiting screen")
    ap.add_argument("--now", type=int, help="device clock, epoch s (default: frame sent_at)")
    ap.add_argument("--view", choices=["main", "detail"], default="main")
    ap.add_argument("--frame-age", type=int, default=0, help="s since last accepted frame (link dot)")
    ap.add_argument("--uptime", type=int, default=3600)
    ap.add_argument("--heap", type=int, default=150000)
    ap.add_argument("--toast")
    ap.add_argument("--via", choices=["usb", "wifi"], default="usb", help="transport of last frame (footer)")
    ap.add_argument("--provisioned", action="store_true")
    ap.add_argument("--wifi", choices=["off", "connecting", "connected"], default="off")
    ap.add_argument("--host", default="ctmeter-a1b2")
    ap.add_argument("--ip", default="")
    ap.add_argument("--rssi", type=int, default=0)
    ap.add_argument("--scale", type=int, default=3, help="nearest-neighbour upscale (1 = native)")
    ap.add_argument("-o", "--out", type=Path, default=OUT_DIR / "preview.png")
    ap.add_argument("--samples", action="store_true", help=f"write sample set to {OUT_DIR}")
    a = ap.parse_args()

    load_fonts()
    if a.samples:
        return samples(a.scale)
    m = Model(view=a.view, frame_age=a.frame_age, uptime=a.uptime, free_heap=a.heap, toast=a.toast,
              via_wifi=a.via == "wifi", provisioned=a.provisioned, wifi=a.wifi, host=a.host,
              ip=a.ip if a.wifi == "connected" else "", rssi=a.rssi if a.wifi == "connected" else 0)
    if a.frame:
        d = json.loads(Path(a.frame).read_text())
        if "line" in d:   # fixtures/frame_*.json wrap the raw frame line
            d = json.loads(d["line"])
        f = parse_frame(d)
        m.have_frame, m.frame, m.last_seq = True, f, f.seq
        m.now = a.now if a.now is not None else f.sent_at
    else:
        m.now = a.now if a.now is not None else int(time.time())
    save(render(m), a.out, a.scale)


if __name__ == "__main__":
    main()
