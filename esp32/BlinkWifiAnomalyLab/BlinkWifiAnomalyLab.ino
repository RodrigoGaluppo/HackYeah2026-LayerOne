#include <Arduino.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <mbedtls/sha256.h>

// LayerOne button-triggered side-channel experiment.
// Baseline: slow heartbeat blink with Wi-Fi off.
// Press GPIO4 to GND to start one bounded, rapid-blink workload cycle.

static constexpr uint8_t LED_PIN = 19;
static constexpr uint8_t KEY1_ROW_PIN = 4;
static constexpr uint8_t KEY1_COLUMN_PIN = 18;
static constexpr uint32_t BUTTON_DEBOUNCE_MS = 40;
static constexpr uint32_t BASELINE_LED_PERIOD_MS = 10000;
static constexpr uint32_t BASELINE_LED_ON_MS = 1000;
static constexpr uint32_t ANOMALY_DURATION_MS = 8000;
static constexpr uint32_t ANOMALY_LED_HALF_PERIOD_MS = 40;
static constexpr uint32_t FLASH_WRITE_INTERVAL_MS = 250;
static constexpr uint32_t MAX_FLASH_WRITES = 24;
static constexpr float MAX_INTERNAL_TEMP_C = 75.0f;

static const char *ANOMALY_AP = "LayerOne-Controlled-Anomaly";
static const char *ANOMALY_PASSWORD = "lab-only-2026";

WiFiUDP udp;
volatile bool anomalyActive = false;
volatile uint32_t scanCount = 0;
volatile uint32_t packetCount = 0;
volatile uint32_t hashCount = 0;
volatile uint32_t ramPassCount = 0;
volatile uint32_t flashWriteCount = 0;
uint32_t anomalyStartedAt = 0;
bool previousLed = false;
bool lastRawButton = HIGH;
bool stableButton = HIGH;
uint32_t buttonChangedAt = 0;

void computeAnomalyWorker(void *argument) {
  const uint32_t core = static_cast<uint32_t>(reinterpret_cast<uintptr_t>(argument));
  static uint32_t ram0[2048];
  static uint32_t ram1[2048];
  uint32_t *ram = core == 0 ? ram0 : ram1;
  uint8_t input[1024];
  uint8_t digest[32];
  uint32_t state = 0xA5A5A5A5U ^ core;

  for (size_t index = 0; index < sizeof(input); ++index) {
    input[index] = static_cast<uint8_t>((index * 73U) ^ (core * 0x5AU));
  }

  for (;;) {
    if (!anomalyActive) {
      vTaskDelay(pdMS_TO_TICKS(25));
      continue;
    }

    // Repeated changing-input SHA-256 resembles sustained cryptomining or
    // unauthorized integrity-scanning activity without performing an attack.
    for (uint8_t round = 0; round < 24; ++round) {
      input[(state + round * 17U) & 1023U] ^= static_cast<uint8_t>(state);
      mbedtls_sha256(input, sizeof(input), digest, 0);
      state ^= static_cast<uint32_t>(digest[round & 31U]) << ((round & 3U) * 8U);
      hashCount++;
    }

    // Exercise the RAM bus with high-transition patterns and dependent reads.
    for (size_t index = 0; index < 2048; ++index) ram[index] = (index ^ state) * 2654435761U;
    for (size_t index = 0; index < 2048; ++index) ram[index] ^= ram[(index + 127U) & 2047U];
    ramPassCount += 2;

    // A real delay (rather than taskYIELD) lets each core's idle task run and
    // service the watchdog.  The workload remains sustained and conspicuous,
    // but it should not turn the experiment into a reset-loop demonstration.
    vTaskDelay(pdMS_TO_TICKS(1));
  }
}

void flashAnomalyWorker(void *) {
  uint8_t payload[256];

  for (size_t index = 0; index < sizeof(payload); ++index) {
    payload[index] = static_cast<uint8_t>((index * 131U) ^ 0x6DU);
  }

  for (;;) {
    while (!anomalyActive) vTaskDelay(pdMS_TO_TICKS(25));

    Preferences preferences;
    uint32_t sequence = 0;
    if (!preferences.begin("l1-anomaly", false)) {
      Serial.println("[ANOMALY] NVS flash namespace unavailable");
      while (anomalyActive) vTaskDelay(pdMS_TO_TICKS(100));
      continue;
    }

    // Deliberately bounded to limit flash wear during repeated button tests.
    while (anomalyActive && flashWriteCount < MAX_FLASH_WRITES) {
      sequence++;
      memcpy(payload, &sequence, sizeof(sequence));
      payload[4] ^= static_cast<uint8_t>(sequence);
      const size_t written = preferences.putBytes("burst", payload, sizeof(payload));
      if (written == sizeof(payload)) flashWriteCount++;
      vTaskDelay(pdMS_TO_TICKS(FLASH_WRITE_INTERVAL_MS));
    }

    preferences.end();
    Serial.printf("[ANOMALY] bounded flash burst complete: %lu writes\n", flashWriteCount);
    while (anomalyActive) vTaskDelay(pdMS_TO_TICKS(25));
  }
}

void wifiAnomalyWorker(void *) {
  uint8_t payload[1400];
  for (size_t index = 0; index < sizeof(payload); ++index) {
    payload[index] = static_cast<uint8_t>((index * 73U) ^ 0xA5U);
  }
  const IPAddress localBroadcast(192, 168, 4, 255);

  for (;;) {
    while (!anomalyActive) vTaskDelay(pdMS_TO_TICKS(25));

    WiFi.mode(WIFI_AP_STA);
    WiFi.setTxPower(WIFI_POWER_19_5dBm);
    WiFi.softAP(ANOMALY_AP, ANOMALY_PASSWORD);
    udp.begin(4210);

    Serial.println("[ANOMALY] Wi-Fi enabled at maximum TX power");
    Serial.println("[ANOMALY] Active scans + local UDP bursts started");

    while (anomalyActive) {
      // Active probe requests exercise RF without joining another network.
      WiFi.scanNetworks(false, true, false, 120);
      WiFi.scanDelete();
      scanCount++;

      // Traffic remains on the ESP32's private access point.
      for (uint16_t burst = 0; burst < 120 && anomalyActive; ++burst) {
        payload[0]++;
        payload[1] ^= payload[0];
        payload[2] = static_cast<uint8_t>(millis());
        udp.beginPacket(localBroadcast, 4210);
        udp.write(payload, sizeof(payload));
        udp.endPacket();
        packetCount++;
        taskYIELD();
      }
    }

    udp.stop();
    WiFi.softAPdisconnect(true);
    WiFi.disconnect(true, true);
    WiFi.mode(WIFI_OFF);
    Serial.println("[RECOVERY] Wi-Fi workload stopped");
  }
}

void startAnomaly(uint32_t now) {
  hashCount = 0;
  ramPassCount = 0;
  flashWriteCount = 0;
  scanCount = 0;
  packetCount = 0;
  anomalyStartedAt = now;
  anomalyActive = true;
  Serial.println("[TRIGGER] GPIO4 button pressed — anomaly cycle started");
}

void stopAnomaly(const char *reason) {
  anomalyActive = false;
  digitalWrite(LED_PIN, LOW);
  previousLed = false;
  Serial.printf("[RECOVERY] %s; quiet baseline restored\n", reason);
  Serial.printf(
      "[SUMMARY] hashes=%lu ram_passes=%lu flash_writes=%lu scans=%lu packets=%lu temp=%.1fC\n",
      hashCount, ramPassCount, flashWriteCount, scanCount, packetCount,
      temperatureRead());
}

void setup() {
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
  // Key 1 closes the R1/C1 contacts. Hold R1 LOW and read C1 with the
  // internal pull-up: released=HIGH, pressed=LOW.
  pinMode(KEY1_ROW_PIN, OUTPUT);
  digitalWrite(KEY1_ROW_PIN, LOW);
  pinMode(KEY1_COLUMN_PIN, INPUT_PULLUP);
  digitalWrite(LED_PIN, LOW);

  delay(50);
  lastRawButton = digitalRead(KEY1_COLUMN_PIN);
  stableButton = lastRawButton;
  buttonChangedAt = millis();

  // Establish a genuinely radio-quiet baseline.
  WiFi.disconnect(true, true);
  WiFi.mode(WIFI_OFF);

  xTaskCreatePinnedToCore(
      wifiAnomalyWorker, "controlled-wifi-anomaly", 6144,
      nullptr, 1, nullptr, 0);
  xTaskCreatePinnedToCore(
      computeAnomalyWorker, "hash-ram-core0", 5120,
      reinterpret_cast<void *>(0), 1, nullptr, 0);
  xTaskCreatePinnedToCore(
      computeAnomalyWorker, "hash-ram-core1", 5120,
      reinterpret_cast<void *>(1), 1, nullptr, 1);
  xTaskCreatePinnedToCore(
      flashAnomalyWorker, "bounded-flash-writes", 4096,
      nullptr, 1, nullptr, 1);

  Serial.println("LayerOne button-triggered side-channel experiment");
  Serial.println("[BASELINE] GPIO19 heartbeat: 1 s ON / 9 s OFF; Wi-Fi OFF");
  Serial.println("[KEYPAD] key 1 matrix: R1=GPIO4 LOW, C1=GPIO18 INPUT_PULLUP");
  Serial.println("[ARMED] Press keypad 1 to run an 8-second anomaly cycle");
}

void loop() {
  const uint32_t now = millis();
  const bool ledOn = anomalyActive
      ? (((now - anomalyStartedAt) / ANOMALY_LED_HALF_PERIOD_MS) & 1U) == 0
      : (now % BASELINE_LED_PERIOD_MS) < BASELINE_LED_ON_MS;

  if (ledOn != previousLed) {
    digitalWrite(LED_PIN, ledOn ? HIGH : LOW);
    previousLed = ledOn;
    Serial.printf(
        "[%s] %8lu ms  GPIO19 LED %s\n",
        anomalyActive ? "ANOMALY" : "BASELINE",
        now,
        ledOn ? "ON" : "OFF");
  }

  const bool rawButton = digitalRead(KEY1_COLUMN_PIN);
  if (rawButton != lastRawButton) {
    lastRawButton = rawButton;
    buttonChangedAt = now;
  }
  if (now - buttonChangedAt >= BUTTON_DEBOUNCE_MS && rawButton != stableButton) {
    stableButton = rawButton;
    if (stableButton == LOW && !anomalyActive) startAnomaly(now);
  }

  if (anomalyActive &&
      (now - anomalyStartedAt >= ANOMALY_DURATION_MS ||
       temperatureRead() >= MAX_INTERNAL_TEMP_C)) {
    stopAnomaly(temperatureRead() >= MAX_INTERNAL_TEMP_C
        ? "temperature cutoff reached" : "8-second cycle complete");
  }

  static uint32_t lastStatus = 0;
  if (anomalyActive && now - lastStatus >= 5000) {
    lastStatus = now;
    Serial.printf(
        "[ANOMALY] hashes=%lu ram_passes=%lu flash_writes=%lu scans=%lu packets=%lu temp=%.1fC\n",
        hashCount, ramPassCount, flashWriteCount, scanCount, packetCount,
        temperatureRead());
  }

  delay(1);
}
