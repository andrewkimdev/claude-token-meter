#!/usr/bin/env python3
"""대상 T-Display의 시리얼 포트를 독점(TIOCEXCL)으로 붙잡고 로그를 남긴다.

- 장치는 경로가 아니라 USB 시리얼 번호로 찾는다 (다른 보드와 혼동 방지).
  우선순위: `--serial N` > 환경변수 CTM_USB_SERIAL > <CTM_HOME>/config.json의
  "usb_serial" (`ctm select-board`가 쓴다). 기본값은 없다: 셋 다 없으면 종료한다.
  같은 번호의 포트가 둘 이상이면 아무것도 열지 않는다.
- 열 때 DTR/RTS를 내린 채로 열어 불필요한 리셋을 피한다.
- 업로드 등으로 포트가 필요하면: `kill -USR1 <pid>` → 해제(PAUSED),
  다시 `kill -USR1 <pid>` → 재획득. 상태는 <CTM_HOME>/serial/hold.state
- 로그: <CTM_HOME>/serial/YYYYMMDD.log (라인마다 로컬 타임스탬프)
- CTM_HOME 기본값은 ~/.claude-token-meter. host/ctm 없이 단독으로 돈다.
"""
import json, os, signal, sys, time

ENV_VAR = "CTM_USB_SERIAL"
BAUD = 115200
HOME = os.environ.get("CTM_HOME") or os.path.expanduser("~/.claude-token-meter")
DIR = os.path.join(HOME, "serial")
STATE = os.path.join(DIR, "hold.state")
PID = os.path.join(DIR, "hold.pid")


def resolve_serial(argv=None, environ=None, home=None):
    """--serial N > CTM_USB_SERIAL > config.json usb_serial > None (ctm.board와 같은 규칙)."""
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    for i, a in enumerate(argv):
        if a == "--serial" and i + 1 < len(argv) and argv[i + 1].strip():
            return argv[i + 1].strip()
        if a.startswith("--serial=") and a.split("=", 1)[1].strip():
            return a.split("=", 1)[1].strip()
    v = (environ.get(ENV_VAR) or "").strip()
    if v:
        return v
    try:
        with open(os.path.join(home or HOME, "config.json")) as f:
            d = json.load(f)
        v = str(d.get("usb_serial") or "").strip() if isinstance(d, dict) else ""
    except (OSError, ValueError):
        v = ""
    return v or None


paused = False
def on_usr1(*_):
    global paused
    paused = not paused

def state(s):
    with open(STATE, "w") as f:
        f.write(f"{time.strftime('%F %T')} {s}\n")

def log(line):
    with open(os.path.join(DIR, time.strftime("%Y%m%d") + ".log"), "a") as f:
        f.write(f"{time.strftime('%F %T')} {line}\n")

def find_port(serial_no):
    """(dev, why). 정확히 일치 우선, 없으면 접두 일치. 둘 이상이면 (None, 이유): 추측 금지."""
    from serial.tools import list_ports
    ports = [p for p in list_ports.comports() if p.serial_number and "cu." in p.device]
    m = [p.device for p in ports if p.serial_number == serial_no] or \
        [p.device for p in ports if p.serial_number.startswith(serial_no)]
    if len(m) > 1:
        return None, f"{len(m)} ports match USB serial {serial_no}; refusing to guess"
    return (m[0], None) if m else (None, "device not present")

def open_port(dev):
    import fcntl, termios
    import serial
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = dev, BAUD, 0.5
    s.dtr = False; s.rts = False          # 열 때 리셋 펄스 방지
    s.open()
    # pyserial의 exclusive=True는 권고형 flock이라 flock을 안 쓰는 도구는 못 막는다 [V].
    # TIOCEXCL은 커널 수준: 이후 다른 비-root 프로세스의 open()이 EBUSY로 실패한다.
    fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
    return s

def main():
    serial_no = resolve_serial()
    if not serial_no:
        sys.stderr.write("serial_hold: no board selected. Run `ctm select-board`, or pass "
                         "--serial N / set CTM_USB_SERIAL.\n")
        sys.exit(2)
    signal.signal(signal.SIGUSR1, on_usr1)
    os.makedirs(DIR, exist_ok=True)
    with open(PID, "w") as f: f.write(str(os.getpid()))
    s, buf = None, b""
    while True:
        if paused:
            if s:
                s.close(); s = None
                log("[hold] RELEASED (paused)"); state("PAUSED")
            time.sleep(0.2); continue
        if s is None:
            dev, why = find_port(serial_no)
            if not dev:
                state(f"WAITING ({why})"); time.sleep(1); continue
            try:
                s = open_port(dev)
                log(f"[hold] HELD {dev}"); state(f"HELD {dev}")
            except Exception as e:
                state(f"OPEN FAILED {dev}: {e}"); time.sleep(2); continue
        try:
            chunk = s.read(512)
        except Exception as e:
            log(f"[hold] read error: {e}"); s.close(); s = None; continue
        if chunk:
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                log(line.rstrip(b"\r").decode("utf-8", "replace"))

if __name__ == "__main__":
    main()
