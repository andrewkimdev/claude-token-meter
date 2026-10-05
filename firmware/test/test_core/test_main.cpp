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

#include "core/backoff.h"
#include "core/command.h"
#include "core/hmac.h"
#include "core/http_accept.h"
#include "core/meter.h"
#include "core/persist.h"
#include "core/protocol.h"

#ifndef FIXTURES_DIR
#error "FIXTURES_DIR must be defined (see [env:native] in platformio.ini)"
#endif

using namespace ctm;

void setUp() {}
void tearDown() {}

// ---------------------------------------------------------------- fixtures

static std::vector<std::string> listFixtures(const char *prefix) {
  std::vector<std::string> names;
  const std::string pre = prefix;
  DIR *d = opendir(FIXTURES_DIR);
  if (!d) return names;
  while (dirent *e = readdir(d)) {
    std::string n = e->d_name;
    if (n.rfind(pre, 0) == 0 && n.size() > 5 && n.substr(n.size() - 5) == ".json")
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

// ---------------------------------------------------------------- sha256 / hmac

static std::string hex(const uint8_t *b, size_t n) {
  std::string s(2 * n + 1, '\0');
  toHex(b, n, &s[0]);
  s.resize(2 * n);
  return s;
}

static std::string sha256Hex(const std::string &m) {
  uint8_t d[SHA256_LEN];
  sha256(reinterpret_cast<const uint8_t *>(m.data()), m.size(), d);
  return hex(d, SHA256_LEN);
}

static std::string hmacHex(const std::string &k, const std::string &m) {
  uint8_t d[SHA256_LEN];
  hmacSha256(reinterpret_cast<const uint8_t *>(k.data()), k.size(),
             reinterpret_cast<const uint8_t *>(m.data()), m.size(), d);
  return hex(d, SHA256_LEN);
}

// FIPS 180-2 examples + block-boundary lengths (expected values from hashlib).
static void test_sha256_vectors() {
  TEST_ASSERT_EQUAL_STRING("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
                           sha256Hex("abc").c_str());
  TEST_ASSERT_EQUAL_STRING("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                           sha256Hex("").c_str());
  TEST_ASSERT_EQUAL_STRING("248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
                           sha256Hex("abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq").c_str());
  TEST_ASSERT_EQUAL_STRING("d5e285683cd4efc02d021a5c62014694958901005d6f71e89e0989fac77e4072",
                           sha256Hex(std::string(55, 'x')).c_str());
  TEST_ASSERT_EQUAL_STRING("04c26261370ee7541549d16dee320c723e3fd14671e66a099afe0a377c16888e",
                           sha256Hex(std::string(56, 'x')).c_str());
  TEST_ASSERT_EQUAL_STRING("7ce100971f64e7001e8fe5a51973ecdfe1ced42befe7ee8d5fd6219506b5393c",
                           sha256Hex(std::string(64, 'x')).c_str());
  TEST_ASSERT_EQUAL_STRING("9537c5fdf120482f7d58d25e9ed583f52c02b4e304ea814db1633ad565aed7e9",
                           sha256Hex(std::string(65, 'x')).c_str());
  TEST_ASSERT_EQUAL_STRING("13f05a0b594787f5ecd315edc96141bd3243203d1b7d4f0836f37308b276ba98",
                           sha256Hex(std::string(120, 'x')).c_str());
  // Streaming in odd chunks == one shot.
  Sha256 s;
  std::string a(1000000, 'a');
  for (size_t i = 0; i < a.size(); i += 7777)
    s.update(reinterpret_cast<const uint8_t *>(a.data()) + i, std::min<size_t>(7777, a.size() - i));
  uint8_t d[SHA256_LEN];
  s.finish(d);
  TEST_ASSERT_EQUAL_STRING("cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0",
                           hex(d, SHA256_LEN).c_str());
}

// RFC 4231 test cases 1, 2, 6 (6 = key longer than the block size).
static void test_hmac_rfc4231() {
  TEST_ASSERT_EQUAL_STRING("b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
                           hmacHex(std::string(20, '\x0b'), "Hi There").c_str());
  TEST_ASSERT_EQUAL_STRING("5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
                           hmacHex("Jefe", "what do ya want for nothing?").c_str());
  TEST_ASSERT_EQUAL_STRING("60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54",
                           hmacHex(std::string(131, '\xaa'),
                                   "Test Using Larger Than Block-Size Key - Hash Key First").c_str());
}

static void test_hex_helpers() {
  uint8_t b[2];
  TEST_ASSERT_TRUE(fromHex("aB0f", 4, b, 2));
  TEST_ASSERT_EQUAL_HEX8(0xab, b[0]);
  TEST_ASSERT_EQUAL_HEX8(0x0f, b[1]);
  TEST_ASSERT_FALSE(fromHex("ab0", 3, b, 2));
  TEST_ASSERT_FALSE(fromHex("ag0f", 4, b, 2));
  TEST_ASSERT_FALSE(fromHex(nullptr, 4, b, 2));
  const uint8_t x[3] = {1, 2, 3}, y[3] = {1, 2, 4};
  TEST_ASSERT_TRUE(constTimeEq(x, x, 3));
  TEST_ASSERT_FALSE(constTimeEq(x, y, 3));
  TEST_ASSERT_TRUE(constTimeEq(x, y, 2));
}

// fixtures/hmac_*.json (PROTOCOL §4.4), generated by Python's hmac module.
static void test_hmac_fixture() {
  const char *ctx = g_fixture.c_str();
  std::string text;
  TEST_ASSERT_TRUE_MESSAGE(readFile(std::string(FIXTURES_DIR) + "/" + g_fixture, text), ctx);
  JsonDocument fx;
  TEST_ASSERT_TRUE_MESSAGE(!deserializeJson(fx, text), ctx);
  TEST_ASSERT_TRUE_MESSAGE(fx["key_hex"].is<const char *>() && fx["body"].is<const char *>() &&
                               fx["sig"].is<const char *>() && fx["expected"].is<const char *>(),
                           ctx);
  std::string keyHex = fx["key_hex"].as<std::string>();
  std::string body = fx["body"].as<std::string>();
  std::string sig = fx["sig"].as<std::string>();
  std::string exp = fx["expected"].as<std::string>();
  TEST_ASSERT_TRUE_MESSAGE(exp == "ok" || exp == "sig", ctx);
  uint8_t key[PAIR_KEY_LEN];
  TEST_ASSERT_TRUE_MESSAGE(fromHex(keyHex.c_str(), keyHex.size(), key, PAIR_KEY_LEN), ctx);

  bool ok = verifyHmacHex(key, PAIR_KEY_LEN, reinterpret_cast<const uint8_t *>(body.data()),
                          body.size(), sig.c_str());
  char msg[256];
  std::snprintf(msg, sizeof msg, "%s: verify", ctx);
  TEST_ASSERT_EQUAL_STRING_MESSAGE(exp.c_str(), ok ? "ok" : "sig", msg);

  // The HTTP pipeline agrees at its first step: "sig" fixtures -> 401, and
  // "ok" fixtures never -> 401. A rejected sig moves nothing.
  SeqGate gate;
  gate.commit(5);
  HttpVerdict v = acceptHttpState(key, body.data(), body.size(), sig.empty() ? nullptr : sig.c_str(),
                                  false, 0, gate);
  std::snprintf(msg, sizeof msg, "%s: http status", ctx);
  if (exp == "sig") {
    TEST_ASSERT_EQUAL_INT_MESSAGE(401, v.status, msg);
    TEST_ASSERT_EQUAL_STRING_MESSAGE("{\"t\":\"nak\",\"err\":\"sig\"}", v.body, msg);
    TEST_ASSERT_FALSE_MESSAGE(v.applied, msg);
    TEST_ASSERT_TRUE_MESSAGE(gate.last() == 5, msg);
  } else {
    TEST_ASSERT_NOT_EQUAL_MESSAGE(401, v.status, msg);
  }
}

// ---------------------------------------------------------------- HTTP pipeline

static const uint8_t K1[PAIR_KEY_LEN] = {
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
    0x10, 0x11, 0x12, 0x13, 0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1a, 0x1b, 0x1c, 0x1d, 0x1e, 0x1f};

static std::string signK1(const std::string &body) {
  return hmacHex(std::string(reinterpret_cast<const char *>(K1), PAIR_KEY_LEN), body);
}

static std::string stateBody(uint32_t seq, int64_t sentAt) {
  char b[256];
  std::snprintf(b, sizeof b,
                "{\"t\":\"state\",\"v\":1,\"seq\":%lu,\"sent_at\":%lld,\"windows\":"
                "{\"five_hour\":{\"pct\":52,\"resets_at\":1791186600,\"observed_at\":1791179913}}}",
                static_cast<unsigned long>(seq), static_cast<long long>(sentAt));
  return b;
}

static HttpVerdict post(const std::string &body, const std::string &sig, bool clockSet,
                        int64_t now, SeqGate &g) {
  return acceptHttpState(K1, body.data(), body.size(), sig.c_str(), clockSet, now, g);
}

static void test_http_401_before_parse() {
  SeqGate g;
  g.commit(100);
  // Garbage body with a wrong sig: 401, never 400 (sig is checked first).
  HttpVerdict v = post("not json", std::string(64, '0'), true, 1791180000, g);
  TEST_ASSERT_EQUAL_INT(401, v.status);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"err\":\"sig\"}", v.body);
  // Missing header.
  std::string body = stateBody(101, 1791180000);
  v = acceptHttpState(K1, body.data(), body.size(), nullptr, true, 1791180000, g);
  TEST_ASSERT_EQUAL_INT(401, v.status);
  // Good body signed with another key.
  uint8_t k2[PAIR_KEY_LEN];
  std::memcpy(k2, K1, PAIR_KEY_LEN);
  k2[31] ^= 1;
  v = acceptHttpState(k2, body.data(), body.size(), signK1(body).c_str(), true, 1791180000, g);
  TEST_ASSERT_EQUAL_INT(401, v.status);
  TEST_ASSERT_FALSE(v.applied);
  TEST_ASSERT_TRUE(g.last() == 100);   // bad sig moved nothing
}

static void test_http_400() {
  SeqGate g;
  std::string b = "{\"t\":\"state\",\"v\":1,\"seq\":0,\"sent_at\":1}";
  HttpVerdict v = post(b, signK1(b), false, 0, g);
  TEST_ASSERT_EQUAL_INT(400, v.status);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"seq\":0,\"err\":\"parse\"}", v.body);

  b = "{\"t\":\"state\",\"v\":2,\"seq\":7,\"sent_at\":1}";
  v = post(b, signK1(b), false, 0, g);
  TEST_ASSERT_EQUAL_INT(400, v.status);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"seq\":7,\"err\":\"bad_v\"}", v.body);

  b = "";   // signed empty body
  v = post(b, signK1(b), false, 0, g);
  TEST_ASSERT_EQUAL_INT(400, v.status);

  b = "{\"t\":\"scan\"}";   // commands are USB-only; over HTTP a non-state body is 400
  v = post(b, signK1(b), false, 0, g);
  TEST_ASSERT_EQUAL_INT(400, v.status);
  TEST_ASSERT_FALSE(g.have());

  b = "{\"t\":\"state\",\"v\":1,\"seq\":9,\"sent_at\":1,\"pad\":\"" + std::string(1100, 'x') + "\"}";
  v = post(b, signK1(b), false, 0, g);   // > 1024 B
  TEST_ASSERT_EQUAL_INT(400, v.status);
  TEST_ASSERT_FALSE(g.have());
}

static void test_http_skew() {
  const int64_t now = 1791180000;
  SeqGate g;
  g.commit(10);
  // Strictly newer seq: accepted even when skewed (no deadlock; caller resyncs clock).
  std::string b = stateBody(11, now - 301);
  HttpVerdict v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(200, v.status);
  TEST_ASSERT_TRUE(v.applied);
  b = stateBody(12, now + 100000);
  TEST_ASSERT_EQUAL_INT(200, post(b, signK1(b), true, now, g).status);
  TEST_ASSERT_TRUE(g.last() == 12);
  // seq==1 restart exception IS skew-bounded: a captured old seq=1 frame is refused.
  b = stateBody(1, now - 301);
  v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(409, v.status);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"seq\":1,\"err\":\"skew\"}", v.body);
  TEST_ASSERT_TRUE(g.last() == 12);
  // ...but a fresh seq=1 (within 300 s) is accepted.
  b = stateBody(1, now - 300);
  TEST_ASSERT_EQUAL_INT(200, post(b, signK1(b), true, now, g).status);
  TEST_ASSERT_TRUE(g.last() == 1);
  // Clock not set yet: no skew check.
  SeqGate g2;
  g2.commit(9);
  b = stateBody(1, now - 100000);
  TEST_ASSERT_EQUAL_INT(200, post(b, signK1(b), false, 0, g2).status);
  // Old (non-1) seq is old_seq regardless of skew.
  g.commit(50);
  b = stateBody(3, now - 1000);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"seq\":3,\"err\":\"old_seq\"}",
                           post(b, signK1(b), true, now, g).body);
}

static void test_http_old_seq_and_ok() {
  const int64_t now = 1791180000;
  SeqGate g;
  std::string b = stateBody(1791180017, now);
  HttpVerdict v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(200, v.status);
  TEST_ASSERT_TRUE(v.applied);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"ack\",\"seq\":1791180017}", v.body);
  TEST_ASSERT_TRUE(v.frame.sent_at == now);
  TEST_ASSERT_TRUE(v.frame.five_hour.present && v.frame.five_hour.pct == 52.0f);
  TEST_ASSERT_TRUE(g.last() == 1791180017u);

  // Replay of the same signed body: 409 old_seq, gate unchanged.
  v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(409, v.status);
  TEST_ASSERT_EQUAL_STRING("{\"t\":\"nak\",\"seq\":1791180017,\"err\":\"old_seq\"}", v.body);
  TEST_ASSERT_FALSE(v.applied);
  TEST_ASSERT_TRUE(g.last() == 1791180017u);

  // Trailing newline in the body is fine (sig covers it).
  b = stateBody(1791180018, now) + "\n";
  v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(200, v.status);
  TEST_ASSERT_TRUE(g.last() == 1791180018u);
}

static void test_http_bad_sig_moves_nothing() {
  const int64_t now = 1791180000;
  SeqGate g;
  g.commit(50);
  std::string b = stateBody(4000000000u, now + 99999);
  std::string s = signK1(b);
  s[0] = s[0] == 'a' ? 'b' : 'a';
  HttpVerdict v = post(b, s, true, now, g);
  TEST_ASSERT_EQUAL_INT(401, v.status);
  TEST_ASSERT_FALSE(v.applied);   // caller sets the clock only when applied
  TEST_ASSERT_TRUE(g.last() == 50);
  // The same frame correctly signed is then judged on its merits: newer seq -> 200.
  v = post(b, signK1(b), true, now, g);
  TEST_ASSERT_EQUAL_INT(200, v.status);
  TEST_ASSERT_TRUE(g.last() == 4000000000u);
}

// ---------------------------------------------------------------- commands

static CmdType cmd(const std::string &line, ProvisionArgs &a, bool &ok) {
  return parseCommand(line.data(), line.size(), a, ok);
}

static void test_parse_command() {
  ProvisionArgs a;
  bool ok = true;
  TEST_ASSERT_TRUE(cmd("{\"t\":\"scan\"}", a, ok) == CmdType::Scan);
  TEST_ASSERT_TRUE(cmd("{\"t\":\"info\"}", a, ok) == CmdType::Info);
  TEST_ASSERT_TRUE(cmd("{\"t\":\"unpair\"}", a, ok) == CmdType::Unpair);
  TEST_ASSERT_TRUE(cmd(stateBody(1, 1), a, ok) == CmdType::None);   // -> state parser
  TEST_ASSERT_TRUE(cmd("{\"t\":\"bogus\"}", a, ok) == CmdType::None);
  TEST_ASSERT_TRUE(cmd("garbage", a, ok) == CmdType::None);
  TEST_ASSERT_TRUE(cmd("[1]", a, ok) == CmdType::None);

  const std::string key = "000102030405060708090a0b0c0d0e0f101112131415161718191A1B1C1D1E1F";
  auto prov = [&](const std::string &ssid, const std::string &pass, const std::string &k) {
    return "{\"t\":\"provision\",\"ssid\":\"" + ssid + "\",\"pass\":\"" + pass + "\",\"key\":\"" + k + "\"}";
  };
  TEST_ASSERT_TRUE(cmd(prov("home", "secret123", key), a, ok) == CmdType::Provision);
  TEST_ASSERT_TRUE(ok);
  TEST_ASSERT_EQUAL_STRING("home", a.ssid);
  TEST_ASSERT_EQUAL_STRING("secret123", a.pass);
  TEST_ASSERT_EQUAL_MEMORY(K1, a.key, PAIR_KEY_LEN);
  scrub(a);
  TEST_ASSERT_EQUAL_STRING("", a.pass);

  TEST_ASSERT_TRUE(cmd(prov("open-net", "", key), a, ok) == CmdType::Provision);
  TEST_ASSERT_TRUE(ok);   // empty pass = open network
  TEST_ASSERT_TRUE(cmd(prov(std::string(32, 's'), std::string(64, 'p'), key), a, ok) == CmdType::Provision);
  TEST_ASSERT_TRUE(ok);

  struct { std::string line; } bad[] = {
      {prov("", "secret123", key)},                        // empty ssid
      {prov(std::string(33, 's'), "secret123", key)},      // ssid too long
      {prov("home", "short", key)},                        // pass 1..7
      {prov("home", std::string(65, 'p'), key)},           // pass too long
      {prov("home", "secret123", key.substr(0, 62))},      // key too short
      {prov("home", "secret123", key + "00")},             // key too long
      {prov("home", "secret123", "zz" + key.substr(2))},   // key not hex
      {"{\"t\":\"provision\",\"ssid\":\"home\",\"pass\":\"secret123\"}"},   // no key
      {"{\"t\":\"provision\",\"ssid\":1,\"pass\":\"secret123\",\"key\":\"" + key + "\"}"},
  };
  for (auto &b : bad) {
    ProvisionArgs z;
    TEST_ASSERT_TRUE_MESSAGE(cmd(b.line, z, ok) == CmdType::Provision, b.line.c_str());
    TEST_ASSERT_FALSE_MESSAGE(ok, b.line.c_str());
    TEST_ASSERT_EQUAL_STRING_MESSAGE("", z.pass, b.line.c_str());   // untouched on error
  }
}

static void test_shape_scan() {
  NetInfo n[6];
  auto set = [&](int i, const char *s, int r, bool o) {
    std::snprintf(n[i].ssid, sizeof n[i].ssid, "%s", s);
    n[i].rssi = r;
    n[i].open = o;
  };
  set(0, "home", -70, false);
  set(1, "", -30, true);      // hidden: dropped
  set(2, "cafe", -50, true);
  set(3, "home", -45, false);  // stronger duplicate wins
  set(4, "lab", -90, false);
  set(5, "cafe", -80, true);
  size_t k = shapeScan(n, 6, SCAN_MAX);
  TEST_ASSERT_EQUAL_UINT(3, k);
  TEST_ASSERT_EQUAL_STRING("home", n[0].ssid);
  TEST_ASSERT_EQUAL_INT(-45, n[0].rssi);
  TEST_ASSERT_EQUAL_STRING("cafe", n[1].ssid);
  TEST_ASSERT_EQUAL_STRING("lab", n[2].ssid);
  // Cap.
  NetInfo m[20];
  for (int i = 0; i < 20; i++) {
    std::snprintf(m[i].ssid, sizeof m[i].ssid, "n%02d", i);
    m[i].rssi = -40 - i;
  }
  TEST_ASSERT_EQUAL_UINT(15, shapeScan(m, 20, SCAN_MAX));
  TEST_ASSERT_EQUAL_STRING("n14", m[14].ssid);
}

static void test_hostname_and_info() {
  const uint8_t mac[6] = {0x24, 0x0a, 0xc4, 0x12, 0xA1, 0xB2};
  char h[24];
  hostnameFromMac(mac, h, sizeof h);
  TEST_ASSERT_EQUAL_STRING("ctmeter-a1b2", h);

  InfoFields f;
  f.host = "ctmeter-a1b2";
  f.ip = "192.168.1.23";
  f.rssi = -55;
  f.uptimeS = 123;
  f.lastSeq = 1791180017;
  f.provisioned = true;
  f.reconnects = 3;
  f.discReason = 200;
  f.heapFree = 150000;
  f.heapMin = 98000;
  f.reset = "poweron";
  f.restored = true;
  char buf[INFO_BUF];
  formatInfo(f, "0.3.0", buf, sizeof buf);
  TEST_ASSERT_EQUAL_STRING(
      "{\"t\":\"info\",\"fw\":\"0.3.0\",\"host\":\"ctmeter-a1b2\",\"ip\":\"192.168.1.23\","
      "\"rssi\":-55,\"uptime_s\":123,\"last_seq\":1791180017,\"provisioned\":true,"
      "\"reconnects\":3,\"disc_reason\":200,\"heap_free\":150000,\"heap_min\":98000,"
      "\"reset\":\"poweron\",\"restored\":true}",
      buf);
  f.wifi = "connected";
  formatInfo(f, "0.3.0", buf, sizeof buf);
  JsonDocument d;
  TEST_ASSERT_TRUE(!deserializeJson(d, buf));
  TEST_ASSERT_EQUAL_STRING("connected", d["wifi"].as<const char *>());
  TEST_ASSERT_TRUE(d["provisioned"].as<bool>());
  // worst case still fits INFO_BUF
  f.ip = "192.168.219.106";
  f.host = "ctmeter-8f70";
  f.reset = "deepsleep";
  f.wifi = "connecting";
  f.rssi = -100;
  f.lastSeq = 4294967295u;
  f.uptimeS = 4294967;
  f.reconnects = 4294967295u;
  f.heapFree = f.heapMin = 4294967295u;
  int k = formatInfo(f, "0.3.0", buf, sizeof buf);
  TEST_ASSERT_TRUE(k > 0 && static_cast<size_t>(k) < sizeof buf);
  TEST_ASSERT_TRUE(!deserializeJson(d, buf));
}

// ---------------------------------------------------------------- P5 persist

static StateFrame sampleFrame() {
  StateFrame f;
  f.seq = 1791180017u;
  f.sent_at = 1791180000;
  f.five_hour = {true, 52.5f, 1791186600, 1791179913};
  f.seven_day = {true, 33.33f, 1791511200, 1791179900};
  return f;
}

static void assertSameFrame(const StateFrame &a, const StateFrame &b) {
  TEST_ASSERT_EQUAL_UINT32(a.seq, b.seq);
  TEST_ASSERT_TRUE(a.sent_at == b.sent_at);
  const Window *wa[2] = {&a.five_hour, &a.seven_day}, *wb[2] = {&b.five_hour, &b.seven_day};
  for (int i = 0; i < 2; i++) {
    TEST_ASSERT_EQUAL(wa[i]->present, wb[i]->present);
    if (!wa[i]->present) continue;
    TEST_ASSERT_FLOAT_WITHIN(0.005f, wa[i]->pct, wb[i]->pct);   // quantised to 0.01
    TEST_ASSERT_TRUE(wa[i]->resets_at == wb[i]->resets_at);
    TEST_ASSERT_TRUE(wa[i]->observed_at == wb[i]->observed_at);
  }
}

static void test_blob_roundtrip() {
  uint8_t b[STATE_BLOB_LEN];
  StateFrame f = sampleFrame(), g;
  encodeStateBlob(f, b);
  TEST_ASSERT_TRUE(b[0] == 'C' && b[1] == 'T' && b[2] == 'L' && b[3] == STATE_BLOB_VER);
  TEST_ASSERT_EQUAL_UINT8(3, b[4]);
  TEST_ASSERT_EQUAL_UINT8(0xF1, b[5]);   // 1791180017 = 0x6AC33CF1, little-endian
  TEST_ASSERT_EQUAL_UINT8(0x6A, b[8]);
  TEST_ASSERT_TRUE(decodeStateBlob(b, sizeof b, g));
  assertSameFrame(f, g);

  // one window missing; extremes
  f.five_hour = Window{};
  f.seven_day.pct = 100.0f;
  f.seq = 0xFFFFFFFFu;
  f.sent_at = 0;
  encodeStateBlob(f, b);
  TEST_ASSERT_EQUAL_UINT8(2, b[4]);
  TEST_ASSERT_TRUE(decodeStateBlob(b, sizeof b, g));
  assertSameFrame(f, g);
  TEST_ASSERT_FALSE(g.five_hour.present);

  // nothing present
  f.seven_day = Window{};
  encodeStateBlob(f, b);
  TEST_ASSERT_TRUE(decodeStateBlob(b, sizeof b, g));
  TEST_ASSERT_FALSE(g.five_hour.present || g.seven_day.present);
}

// Re-seal a hand-edited blob so the CRC passes and the field check is what fails.
static void reseal(uint8_t *b) {
  uint32_t c = crc32(b, 53);
  for (int i = 0; i < 4; i++) b[53 + i] = static_cast<uint8_t>(c >> (8 * i));
}

static void test_blob_rejects() {
  uint8_t b[STATE_BLOB_LEN], x[STATE_BLOB_LEN];
  StateFrame f = sampleFrame(), g = sampleFrame();
  g.seq = 77;   // sentinel: must stay untouched on failure
  encodeStateBlob(f, b);
  TEST_ASSERT_FALSE(decodeStateBlob(b, sizeof b - 1, g));
  TEST_ASSERT_FALSE(decodeStateBlob(nullptr, sizeof b, g));
  for (size_t i = 0; i < sizeof b; i++) {   // any single bit flip is caught
    std::memcpy(x, b, sizeof b);
    x[i] ^= 0x04;
    TEST_ASSERT_FALSE_MESSAGE(decodeStateBlob(x, sizeof x, g), "bit flip");
  }
  std::memcpy(x, b, sizeof b); x[3] = 2; reseal(x);                    // version
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  std::memcpy(x, b, sizeof b); x[4] = 7; reseal(x);                    // unknown flag
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  std::memcpy(x, b, sizeof b); std::memset(x + 5, 0, 4); reseal(x);    // seq 0
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  std::memcpy(x, b, sizeof b); x[17] = 0x11; x[18] = 0x27; reseal(x);  // pct 100.01
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  std::memcpy(x, b, sizeof b); x[16] = 0x80; reseal(x);                // negative sent_at
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  std::memset(x, 0xFF, sizeof x);                                      // erased flash
  TEST_ASSERT_FALSE(decodeStateBlob(x, sizeof x, g));
  TEST_ASSERT_EQUAL_UINT32(77, g.seq);
}

static void test_persist_throttle() {
  const uint32_t T0 = 1000, MIN = PERSIST_MIN_INTERVAL_MS, URG = PERSIST_URGENT_MIN_MS;
  PersistThrottle t;
  TEST_ASSERT_FALSE(t.due(T0));                 // nothing pending
  StateFrame a = sampleFrame();
  t.offer(a);
  TEST_ASSERT_TRUE(t.due(T0));                  // first write after boot: immediate
  t.written(T0);
  TEST_ASSERT_FALSE(t.pending());

  StateFrame b = a;                             // pct-only change: throttled
  b.five_hour.pct = 60;
  b.seq++;
  t.offer(b);
  TEST_ASSERT_FALSE(t.urgent());
  TEST_ASSERT_FALSE(t.due(T0 + URG));
  TEST_ASSERT_FALSE(t.due(T0 + MIN - 1));
  StateFrame c = b;                             // newer pending replaces it
  c.five_hour.pct = 61;
  c.seq++;
  t.offer(c);
  TEST_ASSERT_TRUE(t.due(T0 + MIN));
  TEST_ASSERT_EQUAL_UINT32(c.seq, t.pendingFrame().seq);
  const uint32_t T1 = T0 + MIN;
  t.written(T1);

  StateFrame d = c;                             // new 5h window: prompt (30 s floor)
  d.five_hour.resets_at += FIVE_HOUR_S;
  d.five_hour.pct = 1;
  d.seq++;
  t.offer(d);
  TEST_ASSERT_TRUE(t.urgent());
  TEST_ASSERT_FALSE(t.due(T1 + URG - 1));
  StateFrame e = d;                             // pct-only after it: urgency is sticky
  e.five_hour.pct = 2;
  e.seq++;
  t.offer(e);
  TEST_ASSERT_TRUE(t.urgent());
  TEST_ASSERT_TRUE(t.due(T1 + URG));
  t.failed(T1 + URG);                           // NVS refused: keep, retry on 5 min
  TEST_ASSERT_TRUE(t.pending());
  TEST_ASSERT_FALSE(t.due(T1 + URG + MIN - 1));
  TEST_ASSERT_TRUE(t.due(T1 + URG + MIN));
  t.written(T1 + URG + MIN);

  // millis() wrap
  PersistThrottle w;
  w.offer(a);
  w.written(0xFFFFFF00u);
  w.offer(b);
  TEST_ASSERT_FALSE(w.due(0x00000100u));
  TEST_ASSERT_TRUE(w.due(0xFFFFFF00u + MIN));

  // seeded from NVS: same windows -> not urgent; a window appearing -> urgent
  PersistThrottle s;
  StateFrame stored = a;
  stored.seven_day = Window{};
  s.seed(stored);
  s.offer(stored);
  TEST_ASSERT_FALSE(s.urgent());
  TEST_ASSERT_TRUE(s.due(T0));                  // still: first write after boot
  s.written(T0);
  s.offer(a);                                   // seven_day appears
  TEST_ASSERT_TRUE(s.urgent());
  TEST_ASSERT_TRUE(isNewWindow(stored, a));
  TEST_ASSERT_FALSE(isNewWindow(a, stored));    // a window vanishing is not "new"
}

static void test_clock_guards() {
  TEST_ASSERT_FALSE(clockUsable(false, 1791180000));
  TEST_ASSERT_FALSE(clockUsable(true, 37));            // boot epoch ~ uptime
  TEST_ASSERT_FALSE(clockUsable(true, MIN_VALID_EPOCH - 1));
  TEST_ASSERT_TRUE(clockUsable(true, 1791180000));

  Window w{true, 84.4f, 1791186600, 1791179913};
  WindowView v = makeView(w, 37, FIVE_HOUR_S, false);  // unset clock: no time math
  TEST_ASSERT_TRUE(v.state == WinState::NoClock);
  TEST_ASSERT_EQUAL_INT(84, v.pctInt);
  TEST_ASSERT_TRUE(v.remaining == 0);
  TEST_ASSERT_FLOAT_WITHIN(1e-6f, 0.0f, v.pace);
  TEST_ASSERT_TRUE(v.level == Level::Orange);
  w.pct = 50;
  TEST_ASSERT_TRUE(makeView(w, 37, FIVE_HOUR_S, false).level == Level::Neutral);  // no pace, no amber
  w.pct = 96;
  TEST_ASSERT_TRUE(makeView(w, 37, FIVE_HOUR_S, false).level == Level::Red);
  TEST_ASSERT_TRUE(makeView(Window{}, 37, FIVE_HOUR_S, false).state == WinState::Missing);
  // an expired restored window is NOT judged "reset" without a clock
  w.resets_at = 10;
  TEST_ASSERT_TRUE(makeView(w, 37, FIVE_HOUR_S, false).state == WinState::NoClock);

  char buf[16];
  TEST_ASSERT_EQUAL_STRING("--", formatAgeOrDash(false, 37, 1791179913, buf, sizeof buf));
  TEST_ASSERT_EQUAL_STRING("no data", formatAgeOrDash(true, 1791180000, 0, buf, sizeof buf));
  TEST_ASSERT_EQUAL_STRING("1m ago", formatAgeOrDash(true, 1791180000, 1791179913, buf, sizeof buf));
}

static void test_backoff_schedule() {
  Backoff b;
  const uint32_t want[] = {5000, 10000, 20000, 40000, 60000, 60000, 60000};
  for (uint32_t w : want) TEST_ASSERT_EQUAL_UINT32(w, b.next());
  TEST_ASSERT_EQUAL_UINT32(7, b.attempts());
  b.reset();
  TEST_ASSERT_EQUAL_UINT32(0, b.attempts());
  TEST_ASSERT_EQUAL_UINT32(5000, b.next());
  for (int i = 0; i < 1000; i++) TEST_ASSERT_TRUE(b.next() <= REJOIN_CAP_MS);   // never overflows
}

// Gate seeded from a restored last_seq (P5, PROTOCOL §3): exactly the
// fixtures' "last_seq: N" case, i.e. no longer "anything goes" after a reboot.
static void test_seq_gate_seeded_after_restore() {
  uint8_t b[STATE_BLOB_LEN];
  StateFrame f = sampleFrame(), r;
  encodeStateBlob(f, b);
  TEST_ASSERT_TRUE(decodeStateBlob(b, sizeof b, r));
  SeqGate g;
  g.commit(r.seq);
  TEST_ASSERT_FALSE(g.wouldAccept(f.seq));        // replay of the persisted frame
  TEST_ASSERT_FALSE(g.wouldAccept(f.seq - 100));  // older capture
  TEST_ASSERT_TRUE(g.wouldAccept(f.seq + 1));     // epoch-based host seq keeps working
  TEST_ASSERT_TRUE(g.wouldAccept(1));             // P1 seq==1 exception unchanged

  // Over HTTP with the clock still unset (restored, no live frame yet).
  uint8_t key[PAIR_KEY_LEN] = {1};
  char body[128];
  std::snprintf(body, sizeof body, "{\"t\":\"state\",\"v\":1,\"seq\":%lu,\"sent_at\":1791180000}",
                static_cast<unsigned long>(f.seq));
  uint8_t mac[SHA256_LEN];
  hmacSha256(key, sizeof key, reinterpret_cast<const uint8_t *>(body), std::strlen(body), mac);
  char sig[2 * SHA256_LEN + 1];
  toHex(mac, sizeof mac, sig);
  HttpVerdict v = acceptHttpState(key, body, std::strlen(body), sig, false, 37, g);
  TEST_ASSERT_EQUAL_INT(409, v.status);
  TEST_ASSERT_FALSE(v.applied);
  TEST_ASSERT_EQUAL_UINT32(f.seq, g.last());
}

// ---------------------------------------------------------------- runner

int main(int, char **) {
  UNITY_BEGIN();

  std::vector<std::string> fixtures = listFixtures("frame_");
  if (fixtures.empty()) {
    // Fail closed: no fixtures is not a pass (CLAUDE.md rule 6).
    UnityDefaultTestRun([] { TEST_FAIL_MESSAGE("no fixtures/frame_*.json found in " FIXTURES_DIR); },
                        "frame_fixtures_present", __LINE__);
  }
  for (const std::string &n : fixtures) {
    g_fixture = n;
    UnityDefaultTestRun(test_frame_fixture, g_fixture.c_str(), __LINE__);
  }

  std::vector<std::string> hfx = listFixtures("hmac_");
  if (hfx.empty()) {
    UnityDefaultTestRun([] { TEST_FAIL_MESSAGE("no fixtures/hmac_*.json found in " FIXTURES_DIR); },
                        "hmac_fixtures_present", __LINE__);
  }
  for (const std::string &n : hfx) {
    g_fixture = n;
    UnityDefaultTestRun(test_hmac_fixture, g_fixture.c_str(), __LINE__);
  }

  RUN_TEST(test_sha256_vectors);
  RUN_TEST(test_hmac_rfc4231);
  RUN_TEST(test_hex_helpers);
  RUN_TEST(test_http_401_before_parse);
  RUN_TEST(test_http_400);
  RUN_TEST(test_http_skew);
  RUN_TEST(test_http_old_seq_and_ok);
  RUN_TEST(test_http_bad_sig_moves_nothing);
  RUN_TEST(test_parse_command);
  RUN_TEST(test_shape_scan);
  RUN_TEST(test_hostname_and_info);
  RUN_TEST(test_seq_gate_sequence);
  RUN_TEST(test_pace_five_hour);
  RUN_TEST(test_pace_seven_day);
  RUN_TEST(test_level_thresholds);
  RUN_TEST(test_make_view);
  RUN_TEST(test_format_countdown);
  RUN_TEST(test_format_age);
  RUN_TEST(test_latest_observed);
  RUN_TEST(test_blob_roundtrip);
  RUN_TEST(test_blob_rejects);
  RUN_TEST(test_persist_throttle);
  RUN_TEST(test_clock_guards);
  RUN_TEST(test_backoff_schedule);
  RUN_TEST(test_seq_gate_seeded_after_restore);

  return UNITY_END();
}
