#!/usr/bin/env python3
"""P0 spike: statusLine stdin JSON 캡처. 네트워크 I/O 없음, 빠르게 종료.
rate_limits 관련 필드와 메타데이터만 JSONL로 남긴다 (cwd 등 나머지는 키 목록만)."""
import json, os, sys, time

LOG = os.path.expanduser("~/.claude-token-meter/spike/statusline.jsonl")
t0 = time.time()
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("ctm: bad json"); sys.exit(0)
rec = {
    "t": round(t0, 3),
    "session_id": d.get("session_id"),
    "version": d.get("version"),
    "model": (d.get("model") or {}).get("id"),
    "top_keys": sorted(d.keys()),
    "rate_limits": d.get("rate_limits"),
}
try:
    with open(LOG, "a") as f:
        f.write(json.dumps(rec) + "\n")
except Exception:
    pass
rl = d.get("rate_limits") or {}
def pct(k):
    v = (rl.get(k) or {}).get("used_percentage")
    return "--" if v is None else f"{v:.0f}%"
print(f"5h {pct('five_hour')} · 7d {pct('seven_day')}")
