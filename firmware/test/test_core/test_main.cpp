// Native tests for src/core (P2). Frame verdicts come from the SAME
// fixtures/frame_*.json the host tests check against proto.device_accept.
#include <ArduinoJson.h>
#include <dirent.h>
#include <unity.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "core/meter.h"
#include "core/protocol.h"

#ifndef FIXTURES_DIR
#error "FIXTURES_DIR must be defined (see [env:native] in platformio.ini)"
#endif

using namespace ctm;

void setUp() {}
void tearDown() {}

// ---------------------------------------------------------------- fixtures

static std::vector<std::string> listFrameFixtures() {
  std::vector<std::string> names;
  DIR *d = opendir(FIXTURES_DIR);
  if (!d) return names;
  while (dirent *e = readdir(d)) {
    std::string n = e->d_name;
    if (n.rfind("frame_", 0) == 0 && n.size() > 5 && n.substr(n.size() - 5) == ".json")
      names.push_back(n);
  }
  closedir(d);
  std::sort(names.begin(), names.end());
  return names;
}

static bool readFile(const std::string &path, std::string &out) {
  std::ifstream f(path, std::ios::binary);
  if (!f) return false;
  std::stringstream ss;
  ss << f.rdbuf();
  out = ss.str();
  return true;
}

// Mirrors app/main.cpp handleLine(): parse -> nak(parse|bad_v) | ignore,
// then SeqGate -> nak(old_seq) | ack (+commit).
struct Verdict {
  std::string result;   // "ack" | "nak" | "ignored"
  std::string err;
  StateFrame frame;
};

static Verdict deviceVerdict(const std::string &line, SeqGate &gate) {
  Verdict v;
  uint32_t seq = 0;
  ParseStatus st = parseStateFrame(line.c_str(), line.size(), v.frame, seq);
  if (st == ParseStatus::NotState) { v.result = "ignored"; return v; }
  if (st != ParseStatus::Ok) { v.result = "nak"; v.err = errString(st); return v; }
  if (!gate.wouldAccept(v.frame.seq)) { v.result = "nak"; v.err = "old_seq"; return v; }
  gate.commit(v.frame.seq);
  v.result = "ack";
  return v;
}

static std::string g_fixture;   // current fixture file name

static void checkWindow(JsonObjectConst expWins, const char *key, const Window &w,
                        const char *ctx) {
  JsonVariantConst e = expWins[key];
  char msg[256];
  std::snprintf(msg, sizeof msg, "%s: window %s", ctx, key);
  if (e.isNull()) {
    TEST_ASSERT_FALSE_MESSAGE(w.present, msg);
    return;
  }
  TEST_ASSERT_TRUE_MESSAGE(w.present, msg);
  TEST_ASSERT_TRUE_MESSAGE(e["pct"].is<double>() && e["resets_at"].is<double>(), msg);
  TEST_ASSERT_FLOAT_WITHIN_MESSAGE(1e-4f, e["pct"].as<float>(), w.pct, msg);
  TEST_ASSERT_TRUE_MESSAGE(static_cast<int64_t>(e["resets_at"].as<double>()) == w.resets_at, msg);
}

static void test_frame_fixture() {
  const char *ctx = g_fixture.c_str();
  std::string text;
  TEST_ASSERT_TRUE_MESSAGE(readFile(std::string(FIXTURES_DIR) + "/" + g_fixture, text), ctx);

  JsonDocument fx;
  TEST_ASSERT_TRUE_MESSAGE(!deserializeJson(fx, text), ctx);
  TEST_ASSERT_TRUE_MESSAGE(fx["line"].is<const char *>(), ctx);
  JsonObjectConst exp = fx["expected"];
  TEST_ASSERT_TRUE_MESSAGE(!exp.isNull(), ctx);
  const char *expResult = exp["result"];
  TEST_ASSERT_NOT_NULL_MESSAGE(expResult, ctx);

  SeqGate gate;
  JsonVariantConst last = fx["last_seq"];
  if (!last.isNull()) {
    TEST_ASSERT_TRUE_MESSAGE(last.is<uint32_t>(), ctx);
    gate.commit(last.as<uint32_t>());
  }

  Verdict v = deviceVerdict(fx["line"].as<std::string>(), gate);
  char msg[256];
  std::snprintf(msg, sizeof msg, "%s: result", ctx);
  TEST_ASSERT_EQUAL_STRING_MESSAGE(expResult, v.result.c_str(), msg);

  if (v.result == "nak") {
    std::snprintf(msg, sizeof msg, "%s: err", ctx);
    TEST_ASSERT_TRUE_MESSAGE(exp["err"].is<const char *>(), msg);
    TEST_ASSERT_EQUAL_STRING_MESSAGE(exp["err"].as<const char *>(), v.err.c_str(), msg);
    TEST_ASSERT_TRUE_MESSAGE(exp["windows"].isNull(), msg);
  } else if (v.result == "ignored") {
    std::snprintf(msg, sizeof msg, "%s: ignored shape", ctx);
    TEST_ASSERT_TRUE_MESSAGE(exp["err"].isNull() && exp["windows"].isNull(), msg);
  } else {
    std::snprintf(msg, sizeof msg, "%s: ack shape", ctx);
    TEST_ASSERT_TRUE_MESSAGE(exp["err"].isNull(), msg);
    JsonObjectConst wins = exp["windows"];
    TEST_ASSERT_TRUE_MESSAGE(!wins.isNull(), msg);
    for (JsonPairConst kv : wins) {   // expected may only name known windows
      std::snprintf(msg, sizeof msg, "%s: unexpected window key %s", ctx, kv.key().c_str());
      TEST_ASSERT_TRUE_MESSAGE(std::strcmp(kv.key().c_str(), "five_hour") == 0 ||
                                   std::strcmp(kv.key().c_str(), "seven_day") == 0, msg);
    }
    checkWindow(wins, "five_hour", v.frame.five_hour, ctx);
    checkWindow(wins, "seven_day", v.frame.seven_day, ctx);
    std::snprintf(msg, sizeof msg, "%s: gate committed", ctx);
    TEST_ASSERT_TRUE_MESSAGE(gate.have() && gate.last() == v.frame.seq, msg);
  }
}

// ---------------------------------------------------------------- seq gate

static void test_seq_gate_sequence() {
  SeqGate g;
  TEST_ASSERT_TRUE(g.wouldAccept(5));          // fresh boot: anything
  g.commit(5);
  TEST_ASSERT_FALSE(g.wouldAccept(5));
  TEST_ASSERT_FALSE(g.wouldAccept(4));
  TEST_ASSERT_TRUE(g.wouldAccept(6));
  TEST_ASSERT_TRUE(g.wouldAccept(1));          // bridge restart
  g.commit(1);
  TEST_ASSERT_FALSE(g.wouldAccept(1));         // 1 == last is a duplicate
  TEST_ASSERT_TRUE(g.wouldAccept(2));
}

// ---------------------------------------------------------------- meter

static void test_pace_five_hour() {
  const int64_t now = 1791180000;
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, paceFraction(now + FIVE_HOUR_S, now, FIVE_HOUR_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.5f, paceFraction(now + FIVE_HOUR_S / 2, now, FIVE_HOUR_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.8f, paceFraction(now + 3600, now, FIVE_HOUR_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, paceFraction(now, now, FIVE_HOUR_S));
  // clamps
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, paceFraction(now + 2 * FIVE_HOUR_S, now, FIVE_HOUR_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 1.0f, paceFraction(now - 10, now, FIVE_HOUR_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, paceFraction(now + 10, now, 0));
}

static void test_pace_seven_day() {
  const int64_t now = 1791180000;
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, paceFraction(now + SEVEN_DAY_S, now, SEVEN_DAY_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 3.0f / 7.0f, paceFraction(now + 4 * 86400, now, SEVEN_DAY_S));
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 6.0f / 7.0f, paceFraction(now + 86400, now, SEVEN_DAY_S));
}

static void test_level_thresholds() {
  TEST_ASSERT_TRUE(levelFor(50.0f, 0.5f) == Level::Neutral);   // == pace
  TEST_ASSERT_TRUE(levelFor(10.0f, 0.5f) == Level::Neutral);   // below pace
  TEST_ASSERT_TRUE(levelFor(50.5f, 0.5f) == Level::Amber);     // above pace
  TEST_ASSERT_TRUE(levelFor(1.0f, 0.0f) == Level::Amber);
  TEST_ASSERT_TRUE(levelFor(79.9f, 0.9f) == Level::Neutral);
  TEST_ASSERT_TRUE(levelFor(79.9f, 0.5f) == Level::Amber);
  TEST_ASSERT_TRUE(levelFor(80.0f, 0.9f) == Level::Orange);    // >= 80 regardless of pace
  TEST_ASSERT_TRUE(levelFor(94.9f, 1.0f) == Level::Orange);
  TEST_ASSERT_TRUE(levelFor(95.0f, 1.0f) == Level::Red);
  TEST_ASSERT_TRUE(levelFor(100.0f, 0.0f) == Level::Red);
  TEST_ASSERT_TRUE(levelFor(0.0f, 0.0f) == Level::Neutral);
}

static void test_make_view() {
  const int64_t now = 1791180000;
  Window w;
  WindowView v = makeView(w, now, FIVE_HOUR_S);
  TEST_ASSERT_TRUE(v.state == WinState::Missing);

  w.present = true;
  w.pct = 52.5f;
  w.resets_at = now + FIVE_HOUR_S / 2;
  w.observed_at = now - 60;
  v = makeView(w, now, FIVE_HOUR_S);
  TEST_ASSERT_TRUE(v.state == WinState::Live);
  TEST_ASSERT_EQUAL_INT(53, v.pctInt);
  TEST_ASSERT_TRUE(v.remaining == FIVE_HOUR_S / 2);
  TEST_ASSERT_TRUE(v.level == Level::Amber);

  w.pct = 40;
  TEST_ASSERT_TRUE(makeView(w, now, FIVE_HOUR_S).level == Level::Neutral);

  w.resets_at = now;   // reset passed (<= now)
  v = makeView(w, now, FIVE_HOUR_S);
  TEST_ASSERT_TRUE(v.state == WinState::Reset);
  TEST_ASSERT_TRUE(v.level == Level::Neutral);
  w.resets_at = now - 3600;
  TEST_ASSERT_TRUE(makeView(w, now, FIVE_HOUR_S).state == WinState::Reset);
}

static void test_format_countdown() {
  char b[16];
  TEST_ASSERT_EQUAL_STRING("<1m", formatCountdown(0, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("<1m", formatCountdown(59, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("<1m", formatCountdown(-5, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("1m", formatCountdown(60, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("45m", formatCountdown(45 * 60 + 59, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("1h00m", formatCountdown(3600, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("2h13m", formatCountdown(2 * 3600 + 13 * 60 + 30, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("23h59m", formatCountdown(86399, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("1d00h", formatCountdown(86400, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("3d04h", formatCountdown(3 * 86400 + 4 * 3600 + 59 * 60, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("7d00h", formatCountdown(SEVEN_DAY_S, b, sizeof b));
}

static void test_format_age() {
  char b[16];
  TEST_ASSERT_EQUAL_STRING("0s ago", formatAge(-3, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("12s ago", formatAge(12, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("59s ago", formatAge(59, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("1m ago", formatAge(60, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("4m ago", formatAge(4 * 60 + 59, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("3h ago", formatAge(3 * 3600 + 1, b, sizeof b));
  TEST_ASSERT_EQUAL_STRING("2d ago", formatAge(2 * 86400 + 5, b, sizeof b));
}

static void test_latest_observed() {
  StateFrame f;
  TEST_ASSERT_TRUE(latestObserved(f) == 0);
  f.five_hour.present = true;
  f.five_hour.observed_at = 100;
  f.seven_day.present = true;
  f.seven_day.observed_at = 200;
  TEST_ASSERT_TRUE(latestObserved(f) == 200);
  f.seven_day.present = false;
  TEST_ASSERT_TRUE(latestObserved(f) == 100);
}

// ---------------------------------------------------------------- runner

int main(int, char **) {
  UNITY_BEGIN();

  std::vector<std::string> fixtures = listFrameFixtures();
  if (fixtures.empty()) {
    // Fail closed: no fixtures is not a pass (CLAUDE.md rule 6).
    UnityDefaultTestRun([] { TEST_FAIL_MESSAGE("no fixtures/frame_*.json found in " FIXTURES_DIR); },
                        "frame_fixtures_present", __LINE__);
  }
  for (const std::string &n : fixtures) {
    g_fixture = n;
    UnityDefaultTestRun(test_frame_fixture, g_fixture.c_str(), __LINE__);
  }

  RUN_TEST(test_seq_gate_sequence);
  RUN_TEST(test_pace_five_hour);
  RUN_TEST(test_pace_seven_day);
  RUN_TEST(test_level_thresholds);
  RUN_TEST(test_make_view);
  RUN_TEST(test_format_countdown);
  RUN_TEST(test_format_age);
  RUN_TEST(test_latest_observed);

  return UNITY_END();
}
