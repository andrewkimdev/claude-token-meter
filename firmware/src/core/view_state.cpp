#include "view_state.h"

#include "meter.h"

namespace ctm {

LinkState linkStateFor(int64_t secsSinceFrame) {
  if (secsSinceFrame <= LINK_FRESH_S) return LinkState::Fresh;
  if (secsSinceFrame <= LINK_OFFLINE_S) return LinkState::Stale;
  return LinkState::Offline;
}

namespace {
bool sameWindow(const Window &a, const Window &b) {
  if (a.present != b.present) return false;
  if (!a.present) return true;
  return a.pct == b.pct && a.resets_at == b.resets_at;
}
}  // namespace

bool windowsChanged(const StateFrame &a, const StateFrame &b) {
  return !sameWindow(a.five_hour, b.five_hour) || !sameWindow(a.seven_day, b.seven_day);
}

bool UiController::wake(uint32_t nowMs) {
  lastChangeMs_ = nowMs;   // a press counts as activity: restart the dim timer
  if (!dimmed_) return false;
  dimmed_ = false;
  return true;
}

bool UiController::onBtn1Short(uint32_t nowMs) {
  if (wake(nowMs)) return true;
  view_ = (view_ == View::Main) ? View::Detail : View::Main;
  viewSinceMs_ = nowMs;
  return true;
}

bool UiController::onBtn1Long(uint32_t nowMs) {
  if (wake(nowMs)) return true;
  toast_ = "run ctm setup";
  toastSinceMs_ = nowMs;
  return true;
}

bool UiController::onBtn2Short(uint32_t nowMs) {
  if (wake(nowMs)) return true;
  blIdx_ = (blIdx_ + 1) % 3;
  return false;   // backlight only; caller applies backlight()
}

void UiController::onWindowsChanged(uint32_t nowMs) {
  lastChangeMs_ = nowMs;
  dimmed_ = false;
}

bool UiController::tick(uint32_t nowMs) {
  bool redraw = false;
  if (view_ == View::Detail && nowMs - viewSinceMs_ >= DETAIL_TIMEOUT_MS) {
    view_ = View::Main;
    redraw = true;
  }
  if (toast_ && nowMs - toastSinceMs_ >= TOAST_MS) {
    toast_ = nullptr;
    redraw = true;
  }
  if (!dimmed_ && nowMs - lastChangeMs_ >= AUTO_DIM_MS) dimmed_ = true;
  return redraw;
}

}  // namespace ctm
