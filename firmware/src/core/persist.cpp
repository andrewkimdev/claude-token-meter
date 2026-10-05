#include "persist.h"

#include <cmath>
#include <cstring>

namespace ctm {

namespace {

constexpr size_t OFF_FLAGS = 4, OFF_SEQ = 5, OFF_SENT = 9, OFF_W5 = 17, OFF_W7 = 35,
                 OFF_CRC = 53, WIN_LEN = 18;

void putU16(uint8_t *p, uint16_t v) {
  p[0] = static_cast<uint8_t>(v);
  p[1] = static_cast<uint8_t>(v >> 8);
}
void putU32(uint8_t *p, uint32_t v) {
  for (int i = 0; i < 4; i++) p[i] = static_cast<uint8_t>(v >> (8 * i));
}
void putI64(uint8_t *p, int64_t s) {
  uint64_t v = static_cast<uint64_t>(s);
  for (int i = 0; i < 8; i++) p[i] = static_cast<uint8_t>(v >> (8 * i));
}
uint16_t getU16(const uint8_t *p) { return static_cast<uint16_t>(p[0] | (p[1] << 8)); }
uint32_t getU32(const uint8_t *p) {
  uint32_t v = 0;
  for (int i = 3; i >= 0; i--) v = (v << 8) | p[i];
  return v;
}
int64_t getI64(const uint8_t *p) {
  uint64_t v = 0;
  for (int i = 7; i >= 0; i--) v = (v << 8) | p[i];
  return static_cast<int64_t>(v);
}

void putWindow(uint8_t *p, const Window &w) {
  if (!w.present) {
    std::memset(p, 0, WIN_LEN);
    return;
  }
  double c = std::lround(static_cast<double>(w.pct) * 100.0);
  if (c < 0) c = 0;
  if (c > 10000) c = 10000;
  putU16(p, static_cast<uint16_t>(c));
  putI64(p + 2, w.resets_at);
  putI64(p + 10, w.observed_at);
}

bool getWindow(const uint8_t *p, bool present, Window &w) {
  w = Window{};
  if (!present) return true;
  uint16_t c = getU16(p);
  int64_t r = getI64(p + 2), o = getI64(p + 10);
  if (c > 10000 || r < 0 || o < 0) return false;
  w.present = true;
  w.pct = static_cast<float>(c) / 100.0f;
  w.resets_at = r;
  w.observed_at = o;
  return true;
}

}  // namespace

uint32_t crc32(const uint8_t *p, size_t n) {
  uint32_t c = 0xFFFFFFFFu;
  for (size_t i = 0; i < n; i++) {
    c ^= p[i];
    for (int k = 0; k < 8; k++) c = (c >> 1) ^ (0xEDB88320u & (0u - (c & 1u)));
  }
  return ~c;
}

void encodeStateBlob(const StateFrame &f, uint8_t out[STATE_BLOB_LEN]) {
  std::memset(out, 0, STATE_BLOB_LEN);
  out[0] = 'C';
  out[1] = 'T';
  out[2] = 'L';
  out[3] = STATE_BLOB_VER;
  out[OFF_FLAGS] = static_cast<uint8_t>((f.five_hour.present ? 1 : 0) |
                                        (f.seven_day.present ? 2 : 0));
  putU32(out + OFF_SEQ, f.seq);
  putI64(out + OFF_SENT, f.sent_at);
  putWindow(out + OFF_W5, f.five_hour);
  putWindow(out + OFF_W7, f.seven_day);
  putU32(out + OFF_CRC, crc32(out, OFF_CRC));
}

bool decodeStateBlob(const uint8_t *in, size_t len, StateFrame &out) {
  if (!in || len != STATE_BLOB_LEN) return false;
  if (in[0] != 'C' || in[1] != 'T' || in[2] != 'L' || in[3] != STATE_BLOB_VER) return false;
  if (getU32(in + OFF_CRC) != crc32(in, OFF_CRC)) return false;
  uint8_t flags = in[OFF_FLAGS];
  if (flags & ~3u) return false;
  StateFrame f;
  f.seq = getU32(in + OFF_SEQ);
  f.sent_at = getI64(in + OFF_SENT);
  if (f.seq == 0 || f.sent_at < 0) return false;
  if (!getWindow(in + OFF_W5, flags & 1, f.five_hour)) return false;
  if (!getWindow(in + OFF_W7, flags & 2, f.seven_day)) return false;
  out = f;
  return true;
}

bool isNewWindow(const StateFrame &stored, const StateFrame &f) {
  auto one = [](const Window &s, const Window &w) {
    return w.present && (!s.present || s.resets_at != w.resets_at);
  };
  return one(stored.five_hour, f.five_hour) || one(stored.seven_day, f.seven_day);
}

void PersistThrottle::seed(const StateFrame &stored) {
  base_ = stored;
  haveBase_ = true;
}

void PersistThrottle::offer(const StateFrame &f) {
  pend_ = f;
  pending_ = true;
  // Sticky until flushed: a later pct-only change must not demote it.
  if (!haveBase_ || isNewWindow(base_, f)) urgent_ = true;
}

bool PersistThrottle::due(uint32_t nowMs) const {
  if (!pending_) return false;
  if (!wroteOnce_) return true;
  uint32_t since = nowMs - lastWriteMs_;
  if (urgent_) return since >= PERSIST_URGENT_MIN_MS;
  return since >= PERSIST_MIN_INTERVAL_MS;
}

void PersistThrottle::written(uint32_t nowMs) {
  base_ = pend_;
  haveBase_ = true;
  pending_ = false;
  urgent_ = false;
  wroteOnce_ = true;
  lastWriteMs_ = nowMs;
}

void PersistThrottle::failed(uint32_t nowMs) {
  urgent_ = false;   // retry on the normal interval
  wroteOnce_ = true;
  lastWriteMs_ = nowMs;
}

}  // namespace ctm
