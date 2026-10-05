#pragma once
#include <Arduino.h>
#include <TFT_eSPI.h>

constexpr int SCR_W = 240, SCR_H = 135;   // rotation 1 (landscape)

extern TFT_eSPI    tft;
extern TFT_eSprite spr;

// Init TFT, verify font 7, allocate the full-screen sprite. Call early in
// setup(), before anything heap-heavy. Returns false (with a message already
// on screen and Serial) on failure; caller must not use `spr` then.
bool displayInit();
