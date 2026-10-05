#!/usr/bin/env python3
"""Regenerate fixtures/hmac_*.json (PROTOCOL §4.4) with Python's hmac module.

Format: {"description","key_hex","body","sig","expected": "ok"|"sig"}.
The firmware native tests (firmware/test/test_core) verify every file.
Keys here are fixed test patterns, never a real pairing key.

  /usr/bin/python3 tools/gen_hmac_fixtures.py
"""
import hashlib
import hmac
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KEY = bytes(range(0x00, 0x20))
KEY2 = bytes(range(0x20, 0x40))
BODY = ('{"t":"state","v":1,"seq":1791180017,"sent_at":1791180000,"windows":'
        '{"five_hour":{"pct":52,"resets_at":1791186600,"observed_at":1791179913},'
        '"seven_day":{"pct":29,"resets_at":1791511200,"observed_at":1791179913}}}')


def sig(k: bytes, b: str) -> str:
    return hmac.new(k, b.encode(), hashlib.sha256).hexdigest()


def flip_last(s: str) -> str:
    return s[:-1] + ("0" if s[-1] != "0" else "1")


CASES = {
    "hmac_ok": ("valid signature over the exact body bytes", KEY, BODY, sig(KEY, BODY), "ok"),
    "hmac_ok_trailing_newline": ("body with trailing newline; the sig covers the newline too",
                                 KEY, BODY + "\n", sig(KEY, BODY + "\n"), "ok"),
    "hmac_wrong_sig": ("valid sig for the body with its last hex digit flipped",
                       KEY, BODY, flip_last(sig(KEY, BODY)), "sig"),
    "hmac_wrong_key": ("signed with a different key", KEY, BODY, sig(KEY2, BODY), "sig"),
    "hmac_body_tampered": ("sig of the original body, but pct changed 52 -> 12",
                           KEY, BODY.replace('"pct":52', '"pct":12'), sig(KEY, BODY), "sig"),
    "hmac_missing_sig": ("no X-CTM-Sig header (empty string)", KEY, BODY, "", "sig"),
    "hmac_empty_body": ("empty body with its correct sig: the signature layer accepts "
                        "(parse rejects later)", KEY, "", sig(KEY, ""), "ok"),
    "hmac_uppercase_sig": ("correct MAC but uppercase hex: PROTOCOL 4.2 requires lowercase",
                           KEY, BODY, sig(KEY, BODY).upper(), "sig"),
    "hmac_truncated_sig": ("first 63 hex chars of the correct sig",
                           KEY, BODY, sig(KEY, BODY)[:63], "sig"),
}


def main() -> None:
    for name, (desc, key, body, s, exp) in CASES.items():
        d = {"description": desc, "key_hex": key.hex(), "body": body, "sig": s, "expected": exp}
        path = ROOT / "fixtures" / f"{name}.json"
        path.write_text(json.dumps(d, indent=2) + "\n")
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
