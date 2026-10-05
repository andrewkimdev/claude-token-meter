#include "buttons.h"

Button button1 = {BUTTON1_PIN, HIGH, HIGH, 0, 0, false};
Button button2 = {BUTTON2_PIN, HIGH, HIGH, 0, 0, false};

void buttonsInit() {
  pinMode(BUTTON1_PIN, INPUT);          // external pull-up already on board
  pinMode(BUTTON2_PIN, INPUT_PULLUP);
}

PressKind checkPress(Button &b) {
  bool raw = digitalRead(b.pin);
  uint32_t now = millis();
  if (raw != b.lastRaw) {
    b.lastRaw = raw;
    b.lastChangeMs = now;
  }
  if (raw != b.stable && now - b.lastChangeMs >= DEBOUNCE_MS) {
    bool wasReleased = (b.stable == HIGH);
    b.stable = raw;
    if (wasReleased && b.stable == LOW) {
      b.pressStartMs = now;   // press edge
      b.longFired = false;
    } else if (!wasReleased && b.stable == HIGH) {
      if (!b.longFired) return PRESS_SHORT;   // release edge
    }
  }
  if (b.stable == LOW && !b.longFired && now - b.pressStartMs >= LONG_PRESS_MS) {
    b.longFired = true;
    return PRESS_LONG;
  }
  return PRESS_NONE;
}
