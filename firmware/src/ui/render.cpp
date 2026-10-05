#include "render.h"

#include <time.h>

#include "../core/meter.h"
#include "display.h"

using namespace ctm;

namespace {

// RGB565. Picked, not specified by the spec.
constexpr uint16_t C_TEXT    = TFT_WHITE;
constexpr uint16_t C_NEUTRAL = 0xBDF7;   // light grey bar fill
constexpr uint16_t C_DIM     = 0x7BEF;   // grey labels / stale
constexpr uint16_t C_TRACK   = 0x2945;   // empty bar track
constexpr uint16_t C_AMBER   = 0xFE60;   // ~ (255,204,0)
constexpr uint16_t C_ORANGE  = 0xFC00;   // ~ (255,128,0)
constexpr uint16_t C_RED     = 0xF800;
constexpr uint16_t C_TICK    = 0x07FF;   // cyan pace tick
constexpr uint16_t C_GREEN   = 0x07E0;

constexpr int PAD = 4;
constexpr int RIGHT = SCR_W - PAD;   // 236
constexpr int BAR_X = PAD, BAR_W = SCR_W - 2 * PAD;   // 4..236

// 5H block
constexpr int Y5_PCT = 4;     // font 7 (48px): 4..52
constexpr int Y5_LBL = 4;     // font 4 label, right column
constexpr int Y5_CD  = 30;    // font 4 countdown, right column
constexpr int Y5_BAR = 58, H5_BAR = 10;
// 7D block
constexpr int Y7_PCT = 77;    // font 4 (26px): 77..103
constexpr int Y7_BAR = 106, H7_BAR = 6;
// Footer
constexpr int Y_FOOT = 119;   // font 2 (16px)

uint16_t levelColor(Level l) {
  switch (l) {
    case Level::Red:    return C_RED;
    case Level::Orange: return C_ORANGE;
    case Level::Amber:  return C_AMBER;
    default:            return C_TEXT;
  }
}
uint16_t barColor(Level l) { return l == Level::Neutral ? C_NEUTRAL : levelColor(l); }

void drawBar(int y, int h, const WindowView &v) {
  spr.fillRect(BAR_X, y, BAR_W, h, C_TRACK);
  if (v.state != WinState::Live) return;
  int fw = static_cast<int>(BAR_W * v.pct / 100.0f + 0.5f);
  if (fw > 0) spr.fillRect(BAR_X, y, fw, h, barColor(v.level));
  int tx = BAR_X + static_cast<int>(BAR_W * v.pace + 0.5f);
  if (tx > BAR_X + BAR_W - 2) tx = BAR_X + BAR_W - 2;
  spr.fillRect(tx, y - 3, 2, h + 6, C_TICK);
}

// Right-aligned countdown (or "reset") in font 4 at y.
void drawCountdown(int y, const WindowView &v) {
  spr.setTextDatum(TR_DATUM);
  if (v.state == WinState::Live) {
    char buf[16];
    spr.setTextColor(C_TEXT);
    spr.drawString(formatCountdown(v.remaining, buf, sizeof buf), RIGHT, y, 4);
  } else if (v.state == WinState::Reset) {
    spr.setTextColor(C_DIM);
    spr.drawString("reset", RIGHT, y, 4);
  }
  spr.setTextDatum(TL_DATUM);
}

void drawFiveHour(const WindowView &v) {
  // Label, right column.
  spr.setTextDatum(TR_DATUM);
  spr.setTextColor(C_DIM);
  spr.drawString("5H", RIGHT, Y5_LBL, 4);
  spr.setTextDatum(TL_DATUM);
  drawCountdown(Y5_CD, v);

  // Big percent: font 7 has only digits/-/./: so '%' comes from font 4.
  uint16_t col = C_DIM;
  char num[8];
  if (v.state == WinState::Live) {
    col = levelColor(v.level);
    snprintf(num, sizeof num, "%d", v.pctInt);
  } else if (v.state == WinState::Reset) {
    snprintf(num, sizeof num, "0");
  } else {
    snprintf(num, sizeof num, "--");
  }
  spr.setTextColor(col);
  int w = spr.drawString(num, PAD, Y5_PCT, 7);
  if (v.state != WinState::Missing) {
    spr.drawString("%", PAD + w + 4, Y5_PCT + 48 - 26, 4);   // bottom-aligned
  }
  drawBar(Y5_BAR, H5_BAR, v);
}

void drawSevenDay(const WindowView &v) {
  uint16_t col = C_DIM;
  char num[8];
  if (v.state == WinState::Live) {
    col = levelColor(v.level);
    snprintf(num, sizeof num, "%d%%", v.pctInt);
  } else if (v.state == WinState::Reset) {
    snprintf(num, sizeof num, "0%%");
  } else {
    snprintf(num, sizeof num, "--");
  }
  spr.setTextColor(col);
  int w = spr.drawString(num, PAD, Y7_PCT, 4);
  spr.setTextColor(C_DIM);
  spr.drawString("7D", PAD + w + 8, Y7_PCT + 6, 2);
  drawCountdown(Y7_PCT, v);
  drawBar(Y7_BAR, H7_BAR, v);
}

void drawFooter(const RenderModel &m) {
  // Dot: green <=150s since last frame, grey <=300s, red + "host offline" after.
  // Label: transport that delivered the last accepted frame (P4).
  uint16_t dot = C_GREEN, txt = C_TEXT;
  const char *label = m.viaWifi ? "WiFi" : "USB";
  if (m.link == LinkState::Stale) {
    dot = C_DIM; txt = C_DIM;
  } else if (m.link == LinkState::Offline) {
    dot = C_RED; txt = C_RED; label = "host offline";
  }
  spr.fillCircle(PAD + 4, Y_FOOT + 8, 4, dot);
  spr.setTextColor(txt);
  spr.drawString(label, PAD + 12, Y_FOOT, 2);

  int64_t obs = latestObserved(m.frame);
  char buf[16];
  spr.setTextDatum(TR_DATUM);
  spr.setTextColor(C_DIM);
  spr.drawString(obs > 0 ? formatAge(m.now - obs, buf, sizeof buf) : "no data",
                 RIGHT, Y_FOOT, 2);
  spr.setTextDatum(TL_DATUM);
}

// ---- Detail view (BTN1). Font 2 rows, label column + value column. ----
constexpr int DET_Y0 = 2, DET_DY = 16, DET_VX = 76;

void detailRow(int row, const char *label, const char *value, uint16_t col) {
  int y = DET_Y0 + row * DET_DY;
  spr.setTextColor(C_DIM);
  spr.drawString(label, PAD, y, 2);
  spr.setTextColor(col);
  spr.drawString(value, DET_VX, y, 2);
}

// Absolute reset time in local time (TZ env, KST-9 -- gotcha 3.3).
void resetRow(int row, const char *label, const Window &w, int64_t now) {
  char buf[32];
  if (!w.present) {
    detailRow(row, label, "--", C_DIM);
    return;
  }
  time_t t = static_cast<time_t>(w.resets_at);
  struct tm lt;
  localtime_r(&t, &lt);
  if (w.resets_at > now) {
    strftime(buf, sizeof buf, "%a %m-%d %H:%M", &lt);
    detailRow(row, label, buf, C_TEXT);
  } else {
    strftime(buf, sizeof buf, "passed %a %H:%M", &lt);
    detailRow(row, label, buf, C_DIM);
  }
}

// Rows (P4): data age and last seq were dropped for the WiFi rows; data age
// is on the main footer and last_seq is in USB/HTTP info.
void drawDetail(const RenderModel &m) {
  char buf[40], a[16];
  resetRow(0, "5H reset", m.frame.five_hour, m.now);
  resetRow(1, "7D reset", m.frame.seven_day, m.now);

  const char *via = m.viaWifi ? "WiFi" : "USB";
  uint16_t lc = m.link == LinkState::Fresh ? C_GREEN
              : m.link == LinkState::Stale ? C_DIM : C_RED;
  if (m.haveFrame) {
    snprintf(buf, sizeof buf, "%s, frame %s", via, formatAge(m.frameAgeS, a, sizeof a));
    detailRow(2, "link", buf, lc);
  } else {
    detailRow(2, "link", "no frame yet", C_DIM);
  }

  if (m.wifi == WifiState::Connected) {
    snprintf(buf, sizeof buf, "connected, %d dBm", m.rssi);
    detailRow(3, "wifi", buf, C_GREEN);
  } else if (m.wifi == WifiState::Connecting) {
    detailRow(3, "wifi", "connecting...", C_AMBER);
  } else {
    detailRow(3, "wifi", m.provisioned ? "off" : "off (run ctm setup)", C_DIM);
  }

  detailRow(4, "ip", m.ip[0] ? m.ip : "--", m.ip[0] ? C_TEXT : C_DIM);

  snprintf(buf, sizeof buf, "%s.local", m.host);
  detailRow(5, "host", buf, m.provisioned ? C_TEXT : C_DIM);

  snprintf(buf, sizeof buf, "%s / %lu B", formatCountdown(m.uptimeS, a, sizeof a),
           (unsigned long)m.freeHeap);
  detailRow(6, "up / heap", buf, C_TEXT);

  snprintf(buf, sizeof buf, "fw %s  KST", FW_VERSION);
  int y = DET_Y0 + 7 * DET_DY;
  spr.setTextColor(C_DIM);
  spr.drawString(buf, PAD, y, 2);
  spr.setTextDatum(TR_DATUM);
  spr.drawString("BTN1: back", RIGHT, y, 2);
  spr.setTextDatum(TL_DATUM);
}

void drawToast(const char *msg) {
  constexpr int TW = 160, TH = 30;
  int x = (SCR_W - TW) / 2, y = (SCR_H - TH) / 2;
  spr.fillRoundRect(x, y, TW, TH, 6, TFT_BLACK);
  spr.drawRoundRect(x, y, TW, TH, 6, C_TEXT);
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(C_TEXT);
  spr.drawString(msg, SCR_W / 2, SCR_H / 2, 2);
  spr.setTextDatum(TL_DATUM);
}

void drawWaiting(const RenderModel &m) {
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(C_TEXT);
  spr.drawString("Claude Token Meter", SCR_W / 2, 30, 4);
  spr.setTextColor(C_DIM);
  spr.drawString("waiting for host...", SCR_W / 2, 62, 2);
  if (!m.provisioned) {
    spr.drawString("USB serial 115200", SCR_W / 2, 80, 2);
    spr.setTextColor(C_TEXT);
    spr.drawString("USB: run ctm setup", SCR_W / 2, 98, 2);
  } else {
    char buf[40];
    snprintf(buf, sizeof buf, "WiFi: %s.local", m.host);
    spr.drawString(buf, SCR_W / 2, 80, 2);
    if (m.wifi == WifiState::Connected) {
      snprintf(buf, sizeof buf, "connected %s", m.ip);
      spr.setTextColor(C_GREEN);
      spr.drawString(buf, SCR_W / 2, 98, 2);
    } else {
      spr.setTextColor(C_AMBER);
      spr.drawString("connecting...", SCR_W / 2, 98, 2);
    }
  }
  spr.setTextColor(C_DIM);
  char fw[24];
  snprintf(fw, sizeof fw, "fw %s", FW_VERSION);
  spr.drawString(fw, SCR_W / 2, 124, 1);
  spr.setTextDatum(TL_DATUM);
}

}  // namespace

void render(const RenderModel &m) {
  spr.fillSprite(TFT_BLACK);
  spr.setTextDatum(TL_DATUM);
  if (m.view == View::Detail) {
    drawDetail(m);
  } else if (!m.haveFrame) {
    drawWaiting(m);
  } else {
    drawFiveHour(makeView(m.frame.five_hour, m.now, FIVE_HOUR_S));
    drawSevenDay(makeView(m.frame.seven_day, m.now, SEVEN_DAY_S));
    drawFooter(m);
  }
  if (m.toast) drawToast(m.toast);
  spr.pushSprite(0, 0);
}
