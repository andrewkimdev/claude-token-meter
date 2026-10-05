#pragma once
// Pure C++ (no Arduino). Pace, countdown, age and color-level helpers.
#include <cstddef>
#include <cstdint>

#include "protocol.h"

namespace ctm {

constexpr int64_t FIVE_HOUR_S = 5 * 3600;     // 18000
constexpr int64_t SEVEN_DAY_S = 7 * 86400;    // 604800
constexpr int64_t LINK_FRESH_S = 150;          // dot green if a frame arrived within this

enum class Level { Neutral, Amber, Orange, Red };

// NoClock (P5): values restored from NVS while the device clock is unset
// (no RTC, no NTP -- it is set only by an accepted frame's sent_at). pct is
// known; countdown, pace and "reset?" are not, so they are not computed.
enum class WinState { Missing, Reset, Live, NoClock };

// Any epoch below this is "clock never set" (boot epoch is ~0 + uptime).
constexpr int64_t MIN_VALID_EPOCH = 1600000000;   // 2020-09-13

// True only if the clock was set from an accepted frame AND reads plausibly.
// Rendering must not compute countdowns/ages from an unset clock.
bool clockUsable(bool clockSet, int64_t now);

struct WindowView {
  WinState state = WinState::Missing;
  int      pctInt = 0;        // rounded, for display
  float    pct = 0;
  float    pace = 0;          // 0..1 elapsed fraction of the window
  int64_t  remaining = 0;     // seconds until reset (>0 when Live)
  Level    level = Level::Neutral;
};

// elapsed = 1 - (resets_at - now) / windowLen, clamped to [0,1].
float paceFraction(int64_t resetsAt, int64_t now, int64_t windowLen);

// Neutral if pct <= pace%, Amber if above pace, Orange >= 80, Red >= 95.
Level levelFor(float pct, float pace);

// clockOk=false -> NoClock for a present window (pct + level by the 80/95
// thresholds only; pace 0, remaining 0, no reset judgement).
WindowView makeView(const Window &w, int64_t now, int64_t windowLen, bool clockOk = true);

// "<1m", "45m", "2h13m", "3d04h". Negative clamps to 0. Returns buf.
char *formatCountdown(int64_t secs, char *buf, size_t n);

// "12s ago", "4m ago", "3h ago", "2d ago". Negative clamps to 0.
char *formatAge(int64_t secs, char *buf, size_t n);

// Footer age text: "--" if the clock is not usable, "no data" if obs <= 0,
// else formatAge(now - obs). Returns buf.
char *formatAgeOrDash(bool clockOk, int64_t now, int64_t obs, char *buf, size_t n);

// Max observed_at across present windows; 0 if none.
int64_t latestObserved(const StateFrame &f);

}  // namespace ctm
