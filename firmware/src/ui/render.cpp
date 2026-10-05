#include "render.h"

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
  spr.fillCircle(PAD + 4, Y_FOOT + 8, 4, m.linkFresh ? C_GREEN : C_DIM);
  spr.setTextColor(m.linkFresh ? C_TEXT : C_DIM);
  spr.drawString("USB", PAD + 12, Y_FOOT, 2);

  int64_t obs = latestObserved(m.frame);
  char buf[16];
  spr.setTextDatum(TR_DATUM);
  spr.setTextColor(C_DIM);
  spr.drawString(obs > 0 ? formatAge(m.now - obs, buf, sizeof buf) : "no data",
                 RIGHT, Y_FOOT, 2);
  spr.setTextDatum(TL_DATUM);
}

void drawWaiting() {
  spr.setTextDatum(MC_DATUM);
  spr.setTextColor(C_TEXT);
  spr.drawString("Claude Token Meter", SCR_W / 2, 34, 4);
  spr.setTextColor(C_DIM);
  spr.drawString("waiting for host...", SCR_W / 2, 72, 2);
  spr.drawString("USB serial 115200", SCR_W / 2, 92, 2);
  char fw[24];
  snprintf(fw, sizeof fw, "fw %s", FW_VERSION);
  spr.drawString(fw, SCR_W / 2, 124, 1);
  spr.setTextDatum(TL_DATUM);
}

}  // namespace

void render(const RenderModel &m) {
  spr.fillSprite(TFT_BLACK);
  spr.setTextDatum(TL_DATUM);
  if (!m.haveFrame) {
    drawWaiting();
  } else {
    drawFiveHour(makeView(m.frame.five_hour, m.now, FIVE_HOUR_S));
    drawSevenDay(makeView(m.frame.seven_day, m.now, SEVEN_DAY_S));
    drawFooter(m);
  }
  spr.pushSprite(0, 0);
}
