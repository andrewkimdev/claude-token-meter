#include "meter.h"

#include <cmath>
#include <cstdio>

namespace ctm {

float paceFraction(int64_t resetsAt, int64_t now, int64_t windowLen) {
  if (windowLen <= 0) return 0;
  double f = 1.0 - static_cast<double>(resetsAt - now) / static_cast<double>(windowLen);
  if (f < 0) f = 0;
  if (f > 1) f = 1;
  return static_cast<float>(f);
}

Level levelFor(float pct, float pace) {
  if (pct >= 95) return Level::Red;
  if (pct >= 80) return Level::Orange;
  if (pct > pace * 100.0f) return Level::Amber;
  return Level::Neutral;
}

bool clockUsable(bool clockSet, int64_t now) { return clockSet && now >= MIN_VALID_EPOCH; }

WindowView makeView(const Window &w, int64_t now, int64_t windowLen, bool clockOk) {
  WindowView v;
  if (!w.present) return v;
  if (!clockOk) {
    v.state = WinState::NoClock;
    v.pct = w.pct;
    v.pctInt = static_cast<int>(std::lround(w.pct));
    v.level = w.pct >= 95 ? Level::Red : w.pct >= 80 ? Level::Orange : Level::Neutral;
    return v;
  }
  if (w.resets_at <= now) {
    v.state = WinState::Reset;   // estimated: window rolled over since observation
    v.pace = 0;
    return v;
  }
  v.state = WinState::Live;
  v.pct = w.pct;
  v.pctInt = static_cast<int>(std::lround(w.pct));
  v.pace = paceFraction(w.resets_at, now, windowLen);
  v.remaining = w.resets_at - now;
  v.level = levelFor(w.pct, v.pace);
  return v;
}

char *formatCountdown(int64_t secs, char *buf, size_t n) {
  if (secs < 0) secs = 0;
  long s = static_cast<long>(secs);
  if (s < 60) {
    std::snprintf(buf, n, "<1m");
  } else if (s < 3600) {
    std::snprintf(buf, n, "%ldm", s / 60);
  } else if (s < 86400) {
    std::snprintf(buf, n, "%ldh%02ldm", s / 3600, (s % 3600) / 60);
  } else {
    std::snprintf(buf, n, "%ldd%02ldh", s / 86400, (s % 86400) / 3600);
  }
  return buf;
}

char *formatAge(int64_t secs, char *buf, size_t n) {
  if (secs < 0) secs = 0;
  long s = static_cast<long>(secs);
  if (s < 60)         std::snprintf(buf, n, "%lds ago", s);
  else if (s < 3600)  std::snprintf(buf, n, "%ldm ago", s / 60);
  else if (s < 86400) std::snprintf(buf, n, "%ldh ago", s / 3600);
  else                std::snprintf(buf, n, "%ldd ago", s / 86400);
  return buf;
}

char *formatAgeOrDash(bool clockOk, int64_t now, int64_t obs, char *buf, size_t n) {
  if (!clockOk) std::snprintf(buf, n, "--");
  else if (obs <= 0) std::snprintf(buf, n, "no data");
  else formatAge(now - obs, buf, n);
  return buf;
}

int64_t latestObserved(const StateFrame &f) {
  int64_t m = 0;
  if (f.five_hour.present && f.five_hour.observed_at > m) m = f.five_hour.observed_at;
  if (f.seven_day.present && f.seven_day.observed_at > m) m = f.seven_day.observed_at;
  return m;
}

}  // namespace ctm
