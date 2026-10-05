#include "http_accept.h"

#include <cstdio>

namespace ctm {

namespace {

void nak(HttpVerdict &v, int status, uint32_t seq, const char *err) {
  v.status = status;
  std::snprintf(v.body, sizeof v.body, "{\"t\":\"nak\",\"seq\":%lu,\"err\":\"%s\"}",
                static_cast<unsigned long>(seq), err);
}

}  // namespace

HttpVerdict acceptHttpState(const uint8_t *key, const char *body, size_t len,
                            const char *sigHex, bool clockSet, int64_t now,
                            SeqGate &gate) {
  HttpVerdict v;

  // 1. Signature over the raw bytes, before anything looks at the content.
  if (!key || !body ||
      !verifyHmacHex(key, PAIR_KEY_LEN, reinterpret_cast<const uint8_t *>(body), len, sigHex)) {
    v.status = 401;
    std::snprintf(v.body, sizeof v.body, "{\"t\":\"nak\",\"err\":\"sig\"}");
    return v;
  }

  // Trailing newline / whitespace is allowed (§4.2) and not part of the frame.
  size_t n = len;
  while (n && (body[n - 1] == '\n' || body[n - 1] == '\r' || body[n - 1] == ' ' ||
               body[n - 1] == '\t'))
    n--;

  // 2. §3.1 rules. Over HTTP there is no "ignore": a signed non-state body is
  // a malformed request -> 400 parse [?] (spec only defines ignore for USB).
  StateFrame f;
  uint32_t seq = 0;
  if (n > MAX_LINE) {
    nak(v, 400, 0, "parse");
    return v;
  }
  ParseStatus st = parseStateFrame(body, n, f, seq);
  if (st == ParseStatus::NotState) {
    nak(v, 400, 0, "parse");
    return v;
  }
  if (st != ParseStatus::Ok) {
    nak(v, 400, seq, errString(st));
    return v;
  }

  // 3+4. seq gate, with the skew bound applied ONLY where seq can't protect us.
  // A signed frame with seq > last_seq can't be a replay (the gate already
  // saw everything below it), so it is accepted even when skewed and the
  // caller resyncs the clock from it. Rejecting it would deadlock: the device
  // clock is only ever set from accepted frames [P4 review].
  // The skew bound guards the one path that admits a non-increasing seq: the
  // seq==1 bridge-restart exception (a captured seq=1 frame replayed later).
  const bool strictlyNewer = !gate.have() || f.seq > gate.last();
  if (!strictlyNewer) {
    if (!gate.wouldAccept(f.seq)) {
      nak(v, 409, f.seq, "old_seq");
      return v;
    }
    if (clockSet) {
      int64_t d = f.sent_at - now;
      if (d < 0) d = -d;
      if (d > MAX_SKEW_S) {
        nak(v, 409, f.seq, "skew");
        return v;
      }
    }
  }
  gate.commit(f.seq);

  v.status = 200;
  v.applied = true;
  v.frame = f;
  std::snprintf(v.body, sizeof v.body, "{\"t\":\"ack\",\"seq\":%lu}",
                static_cast<unsigned long>(f.seq));
  return v;
}

}  // namespace ctm
