#pragma once
// Pure C++ (no Arduino). P3 UI state: link staleness, which view is shown,
// backlight level (manual cycle + auto-dim), transient toast. Time inputs are
// millis()-style uint32 and wrap-safe (all comparisons use unsigned deltas).
#include <cstdint>

#include "protocol.h"

namespace ctm {

// Footer dot. LINK_FRESH_S (meter.h, 150s) and this bound the three states.
// Heartbeat is 120s (PROTOCOL §3), so Offline means >=2 heartbeats missed.
constexpr int64_t LINK_OFFLINE_S = 300;

enum class LinkState { Fresh, Stale, Offline };

// secsSinceFrame: seconds since the last *accepted* frame (device uptime
// clock, not the host clock). <=150 Fresh, <=300 Stale, else Offline.
LinkState linkStateFor(int64_t secsSinceFrame);

// True if anything the meter shows changed: presence, pct, resets_at.
// observed_at / seq / sent_at are ignored, so heartbeats are not "changes".
bool windowsChanged(const StateFrame &a, const StateFrame &b);

enum class View { Main, Detail };

constexpr uint8_t  BL_LEVELS[3]     = {255, 102, 26};   // 100% / 40% / 10%
constexpr uint8_t  BL_DIM           = 26;               // auto-dim level (10%)
constexpr uint32_t DETAIL_TIMEOUT_MS = 15u * 1000u;
constexpr uint32_t AUTO_DIM_MS       = 60u * 60u * 1000u;
constexpr uint32_t TOAST_MS          = 2000u;

class UiController {
 public:
  // Button events. Return true if the screen needs a redraw.
  // While auto-dimmed, any press only wakes the backlight (press is consumed).
  bool onBtn1Short(uint32_t nowMs);   // toggle Main <-> Detail
  bool onBtn1Long(uint32_t nowMs);    // reserved: toast "setup: P4"
  bool onBtn2Short(uint32_t nowMs);   // cycle backlight 100/40/10 %

  // Call when an accepted frame changed the windows (and for the first frame).
  void onWindowsChanged(uint32_t nowMs);

  // Periodic housekeeping: detail timeout, toast expiry, auto-dim.
  // Returns true if the screen needs a redraw.
  bool tick(uint32_t nowMs);

  View        view() const { return view_; }
  uint8_t     backlight() const { return dimmed_ ? BL_DIM : BL_LEVELS[blIdx_]; }
  bool        dimmed() const { return dimmed_; }
  const char *toast() const { return toast_; }   // nullptr when none

 private:
  bool wake(uint32_t nowMs);   // true if it was dimmed (press consumed)

  View        view_ = View::Main;
  uint32_t    viewSinceMs_ = 0;
  int         blIdx_ = 0;
  bool        dimmed_ = false;
  uint32_t    lastChangeMs_ = 0;
  const char *toast_ = nullptr;
  uint32_t    toastSinceMs_ = 0;
};

}  // namespace ctm
