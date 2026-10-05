#pragma once
// Pure C++ (no Arduino). PROTOCOL §4.2 POST /v1/state accept pipeline:
//   1. HMAC sig (constant time)      -> 401 {"t":"nak","err":"sig"}
//   2. §3.1 parse                     -> 400 {"t":"nak","seq":N,"err":"parse|bad_v"}
//   3. skew (only if clock is set)    -> 409 {"t":"nak","seq":N,"err":"skew"}
//   4. seq gate                       -> 409 {"t":"nak","seq":N,"err":"old_seq"}
//   ok                                -> 200 {"t":"ack","seq":N}
// Only an accepted frame commits the SeqGate; the caller applies the clock
// (settimeofday) only when `applied` is true. So a bad-sig / rejected frame
// moves neither seq nor clock.
#include <cstddef>
#include <cstdint>

#include "hmac.h"
#include "protocol.h"

namespace ctm {

constexpr int64_t MAX_SKEW_S = 300;   // §4.2 step 3

struct HttpVerdict {
  int        status = 500;
  bool       applied = false;   // true only for 200: gate committed, frame valid
  char       body[72] = {0};    // JSON reply (no trailing newline)
  StateFrame frame;             // valid only when applied
};

// key: PAIR_KEY_LEN bytes. body/len: raw request body bytes exactly as
// received (the signature covers these). sigHex: X-CTM-Sig header value, or
// nullptr if absent. clockSet: the device clock was already set from an
// accepted frame (USB or WiFi); now: current device epoch seconds.
HttpVerdict acceptHttpState(const uint8_t *key, const char *body, size_t len,
                            const char *sigHex, bool clockSet, int64_t now,
                            SeqGate &gate);

}  // namespace ctm
