#pragma once
// Adapted from esp32-bus-eta-display src/ui/buttons.*. Debounced, active-low.
// PRESS_SHORT fires on release (before LONG_PRESS_MS); PRESS_LONG fires once
// at the threshold while still held. Poll from loop() only.
#include <Arduino.h>

constexpr int      BUTTON1_PIN   = 35;   // input-only, external pull-up on board
constexpr int      BUTTON2_PIN   = 0;    // internal pull-up; boot-strap pin, fine post-boot
constexpr uint32_t DEBOUNCE_MS   = 40;
constexpr uint32_t LONG_PRESS_MS = 700;

struct Button {
  int      pin;
  bool     stable;
  bool     lastRaw;
  uint32_t lastChangeMs;
  uint32_t pressStartMs;
  bool     longFired;
};

extern Button button1;
extern Button button2;

enum PressKind { PRESS_NONE, PRESS_SHORT, PRESS_LONG };

void      buttonsInit();
PressKind checkPress(Button &b);
