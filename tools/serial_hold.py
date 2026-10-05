#!/usr/bin/env python3
"""대상 T-Display의 시리얼 포트를 독점(TIOCEXCL)으로 붙잡고 로그를 남긴다.

- 장치는 경로가 아니라 USB 시리얼 번호로 찾는다 (다른 보드와 혼동 방지).
- 열 때 DTR/RTS를 내린 채로 열어 불필요한 리셋을 피한다.
- 업로드 등으로 포트가 필요하면: `kill -USR1 <pid>` → 해제(PAUSED),
  다시 `kill -USR1 <pid>` → 재획득. 상태는 ~/.claude-token-meter/serial/hold.state
- 로그: ~/.claude-token-meter/serial/YYYYMMDD.log (라인마다 로컬 타임스탬프)
"""
import fcntl, os, signal, sys, termios, time
import serial
from serial.tools import list_ports

SERIAL_NO = os.environ.get("CTM_USB_SERIAL", "5915018996")
BAUD = 115200
DIR = os.path.expanduser("~/.claude-token-meter/serial")
STATE = os.path.join(DIR, "hold.state")
PID = os.path.join(DIR, "hold.pid")

paused = False
def on_usr1(*_):
    global paused
    paused = not paused
signal.signal(signal.SIGUSR1, on_usr1)

def state(s):
    with open(STATE, "w") as f:
        f.write(f"{time.strftime('%F %T')} {s}\n")

def log(line):
    with open(os.path.join(DIR, time.strftime("%Y%m%d") + ".log"), "a") as f:
        f.write(f"{time.strftime('%F %T')} {line}\n")

def find_port():
    for p in list_ports.comports():
        if p.serial_number and p.serial_number.startswith(SERIAL_NO) and "cu." in p.device:
            return p.device
    return None

def open_port(dev):
    s = serial.Serial()
    s.port, s.baudrate, s.timeout = dev, BAUD, 0.5
    s.dtr = False; s.rts = False          # 열 때 리셋 펄스 방지
    s.open()
    # pyserial의 exclusive=True는 권고형 flock이라 flock을 안 쓰는 도구는 못 막는다 [V].
    # TIOCEXCL은 커널 수준: 이후 다른 비-root 프로세스의 open()이 EBUSY로 실패한다.
    fcntl.ioctl(s.fileno(), termios.TIOCEXCL)
    return s

def main():
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
            dev = find_port()
            if not dev:
                state("WAITING (device not present)"); time.sleep(1); continue
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
