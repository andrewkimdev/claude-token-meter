#pragma once
#include <cstdint>

#include "../core/protocol.h"

struct RenderModel {
  bool             haveFrame = false;   // false -> "waiting for host" screen
  ctm::StateFrame  frame;
  int64_t          now = 0;             // epoch s (system time, set from sent_at)
  bool             linkFresh = false;   // frame accepted within LINK_FRESH_S
};

// Draws into the sprite and pushes it. Main task only (gotcha 3.16).
void render(const RenderModel &m);
