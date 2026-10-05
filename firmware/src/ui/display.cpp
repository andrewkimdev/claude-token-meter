#include "display.h"

#include <esp_arduino_version.h>

TFT_eSPI    tft = TFT_eSPI();
TFT_eSprite spr = TFT_eSprite(&tft);

bool displayInit() {
  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);

  tft.init();
  tft.setRotation(1);
  tft.fillScreen(TFT_BLACK);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("booting...", 4, 4, 2);   // never a silent black screen

  // gotcha 3.1: an uncompiled font draws nothing, silently.
  if (tft.fontHeight(7) == 0 || tft.fontHeight(4) == 0) {
    Serial.println("FATAL: font 7/4 not compiled in");
    tft.drawString("Font 7/4 missing", 4, 60, 2);
    return false;
  }

  // 240*135*2 = 64.8KB; allocate while the heap is unfragmented.
  spr.setColorDepth(16);
  if (!spr.createSprite(SCR_W, SCR_H)) {
    Serial.println("FATAL: sprite allocation failed");
    tft.drawString("Sprite alloc failed", 4, 60, 2);
    return false;
  }
  return true;
}

void setBacklight(uint8_t duty) {
  static bool inited = false;
  static int  last = -1;
  if (duty == last) return;
#if ESP_ARDUINO_VERSION_MAJOR >= 3
  if (!inited) { ledcAttach(TFT_BL, 5000, 8); inited = true; }
  ledcWrite(TFT_BL, duty);
#else
  if (!inited) { ledcSetup(0, 5000, 8); ledcAttachPin(TFT_BL, 0); inited = true; }
  ledcWrite(0, duty);
#endif
  last = duty;
}
