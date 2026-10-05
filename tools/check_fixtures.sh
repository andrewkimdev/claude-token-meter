#!/usr/bin/env bash
# Both sides agree: host unittest + firmware native tests, against the same
# fixtures/. Fail-closed (CLAUDE.md rule 6, gotcha 3.12 corollary): every step
# must succeed and every required file must exist, otherwise no verdict.
# Never touches serial ports: only `pio test -e native` (never the device env).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIO="${PIO:-$HOME/.platformio/penv/bin/pio}"
PY="${PY:-/usr/bin/python3}"

fail() { echo "FAIL: $*" >&2; echo "NO VERDICT (check aborted)" >&2; exit 1; }

# --- preconditions: absence is a failure, not evidence -----------------------
[ -x "$PIO" ] || fail "pio not found/executable: $PIO"
[ -x "$PY" ]  || fail "python not found/executable: $PY"
for f in "$ROOT/host/tests/test_proto.py" "$ROOT/host/ctm/proto.py" \
         "$ROOT/firmware/platformio.ini" "$ROOT/firmware/test/test_core/test_main.cpp"; do
  [ -f "$f" ] || fail "missing $f"
done
n_frame=$(find "$ROOT/fixtures" -maxdepth 1 -type f -name 'frame_*.json' | wc -l | tr -d ' ')
n_merge=$(find "$ROOT/fixtures" -maxdepth 1 -type f -name 'merge_*.json' | wc -l | tr -d ' ')
[ "$n_frame" -gt 0 ] || fail "no fixtures/frame_*.json"
[ "$n_merge" -gt 0 ] || fail "no fixtures/merge_*.json"
grep -q '^\[env:native\]' "$ROOT/firmware/platformio.ini" || fail "[env:native] missing in platformio.ini"
echo "fixtures: $n_frame frame, $n_merge merge"

# --- step 1: host -------------------------------------------------------------
echo "== host unittest"
(cd "$ROOT/host" && "$PY" -m unittest discover -s tests -t .) || fail "host unittest"

# --- step 2: firmware native --------------------------------------------------
echo "== firmware native (pio test -e native)"
log="$(mktemp "${TMPDIR:-/tmp}/ctm_native.XXXXXX")"
trap 'rm -f "$log"' EXIT
"$PIO" test -d "$ROOT/firmware" -e native 2>&1 | tee "$log" || fail "pio test -e native"
# Require that every frame fixture actually ran and passed (no silent skips).
n_pass=$(grep -c 'frame_.*\.json.*\[PASSED\]' "$log" || true)
[ "$n_pass" = "$n_frame" ] || fail "native ran $n_pass/$n_frame frame fixtures"
grep -q 'test cases: .* succeeded' "$log" || fail "native summary not found"
if grep -Eq '\[FAILED\]|[0-9]+ failed' "$log"; then fail "native reported failures"; fi

echo "OK: host and firmware agree on $n_frame frame fixtures"
