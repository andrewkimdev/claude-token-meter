#pragma once
// Pure C++ (no Arduino). P5 last_state persistence:
//   - a fixed-size, endian-explicit blob of the last accepted windows + seq,
//     stored by app/state_store.cpp in NVS ("ctm"/"last"). NVS, not
//     RTC_NOINIT: RTC memory does not survive EN resets (gotcha 3.17).
//   - the flash-wear throttle deciding WHEN to write it.
#include <cstddef>
#include <cstdint>

#include "protocol.h"

namespace ctm {

// Layout v1 (little-endian), 57 bytes:
//   0  'C' 'T' 'L'          magic
//   3  u8   version (1)
//   4  u8   flags (bit0 five_hour present, bit1 seven_day present)
//   5  u32  last_seq (>= 1)
//   9  i64  sent_at
//   17 window five_hour: u16 pct*100 (0..10000), i64 resets_at, i64 observed_at
//   35 window seven_day: same
//   53 u32  CRC-32 (IEEE) over bytes 0..52
// An absent window is stored as zeros. pct is quantised to 0.01.
constexpr size_t  STATE_BLOB_LEN = 57;
constexpr uint8_t STATE_BLOB_VER = 1;

// Always succeeds for a frame that passed parseStateFrame (pct is clamped).
void encodeStateBlob(const StateFrame &f, uint8_t out[STATE_BLOB_LEN]);

// Fail-closed: wrong length / magic / version / CRC / flags, seq == 0,
// negative epochs or pct > 100 -> false and `out` untouched.
bool decodeStateBlob(const uint8_t *in, size_t len, StateFrame &out);

uint32_t crc32(const uint8_t *p, size_t n);

// ---- write throttle -------------------------------------------------------
// At most one write per PERSIST_MIN_INTERVAL_MS, except when a window's
// resets_at changed (or a window appeared) versus what is stored: a new
// window is written promptly (but never more often than PERSIST_URGENT_MIN_MS,
// so a misbehaving host cannot hammer flash). The latest offered frame is
// kept in RAM and flushed when allowed. The first write after boot is
// immediate. Times are millis()-style uint32, wrap-safe.
constexpr uint32_t PERSIST_MIN_INTERVAL_MS = 5u * 60u * 1000u;
constexpr uint32_t PERSIST_URGENT_MIN_MS   = 30u * 1000u;

class PersistThrottle {
 public:
  // What NVS already holds (restored at boot). Not a write: does not start
  // the interval. Only used to decide whether a later offer is a new window.
  void seed(const StateFrame &stored);

  // An accepted frame whose windows changed. Replaces any pending frame.
  void offer(const StateFrame &f);

  bool pending() const { return pending_; }
  bool urgent() const { return urgent_; }
  const StateFrame &pendingFrame() const { return pend_; }

  // True if pending and a write is allowed now.
  bool due(uint32_t nowMs) const;

  // Report the outcome of the write of pendingFrame(). A failed write keeps
  // the frame pending but still consumes the interval (retry later, never spin).
  void written(uint32_t nowMs);
  void failed(uint32_t nowMs);

 private:
  bool       haveBase_ = false;
  StateFrame base_;          // last content known to be in NVS
  bool       pending_ = false;
  bool       urgent_ = false;
  StateFrame pend_;
  bool       wroteOnce_ = false;
  uint32_t   lastWriteMs_ = 0;
};

// True if `f` starts a new window versus `stored`: some window present in
// `f` is absent in `stored` or has a different resets_at.
bool isNewWindow(const StateFrame &stored, const StateFrame &f);

}  // namespace ctm
