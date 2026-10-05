#pragma once
// Pure C++ (no Arduino). P5 WiFi rejoin schedule: 5 s, 10 s, 20 s, 40 s,
// then 60 s forever. reset() on a successful connection. No ESP.restart()
// anywhere on this path (gotcha 3.7).
#include <cstdint>

namespace ctm {

constexpr uint32_t REJOIN_FIRST_MS = 5u * 1000u;
constexpr uint32_t REJOIN_CAP_MS   = 60u * 1000u;

class Backoff {
 public:
  explicit Backoff(uint32_t firstMs = REJOIN_FIRST_MS, uint32_t capMs = REJOIN_CAP_MS)
      : first_(firstMs), cap_(capMs), cur_(firstMs) {}

  // Delay before the next attempt; advances the schedule.
  uint32_t next() {
    uint32_t d = cur_;
    cur_ = (cur_ >= cap_ / 2) ? cap_ : cur_ * 2;
    attempts_++;
    return d;
  }
  void reset() {
    cur_ = first_;
    attempts_ = 0;
  }
  uint32_t attempts() const { return attempts_; }

 private:
  uint32_t first_, cap_, cur_;
  uint32_t attempts_ = 0;
};

}  // namespace ctm
