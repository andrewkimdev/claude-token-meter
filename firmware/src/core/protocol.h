#pragma once
// Pure C++ (no Arduino). PROTOCOL.md §3 state frame parsing + seq rules.
// ArduinoJson is header-only and portable, so native tests can use it too.
#include <cstddef>
#include <cstdint>

namespace ctm {

constexpr int PROTOCOL_VERSION = 1;
constexpr const char *FW_VERSION = "0.1.0";
constexpr const char *BOARD = "t-display";
constexpr size_t MAX_LINE = 1024;   // §4.1

struct Window {
  bool    present = false;
  float   pct = 0;          // clamped to 0..100
  int64_t resets_at = 0;    // epoch s
  int64_t observed_at = 0;  // epoch s
};

struct StateFrame {
  uint32_t seq = 0;
  int64_t  sent_at = 0;
  Window   five_hour;
  Window   seven_day;
};

enum class ParseStatus {
  Ok,
  NotState,   // valid JSON object but t != "state": ignored (no ack/nak)
  Parse,      // nak err=parse
  BadV,       // nak err=bad_v
};

// Parses one line (without trailing newline). On Parse/BadV, `seqOut` holds
// the frame's seq if it could be read, else 0.
ParseStatus parseStateFrame(const char *line, size_t len, StateFrame &out,
                            uint32_t &seqOut);

// §3 seq rules. First frame after boot is always accepted. Otherwise
// accept seq > last, or the bridge-restart case (seq == 1 && seq < last).
class SeqGate {
 public:
  bool wouldAccept(uint32_t seq) const;
  void commit(uint32_t seq) { have_ = true; last_ = seq; }
  bool have() const { return have_; }
  uint32_t last() const { return last_; }
 private:
  bool have_ = false;
  uint32_t last_ = 0;
};

const char *errString(ParseStatus s);   // "parse" | "bad_v" | ""

}  // namespace ctm
