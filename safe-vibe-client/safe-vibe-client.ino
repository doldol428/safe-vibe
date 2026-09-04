/*
 * safe-vibe-client — Arduino UNO R4 WiFi
 *
 *   safe-vibe (app.py) --MQTT--> mosquitto --> 이 보드 --> 진동 모터(D7)
 *
 * 구독 토픽 : safe-vibe/alert   (QoS 1)
 * 페이로드   : {"ts":"14:03:22","roi_id":1,"roi_name":"입구","track_id":7,
 *               "name":"person","dwell":2.1,"event":"roi_dwell","ts_epoch":1788504449.5}
 *
 * 필요한 라이브러리 (라이브러리 매니저에서 설치):
 *   - ArduinoMqttClient   (Arduino 공식)
 *   - ArduinoJson         (Benoit Blanchon, v7)
 *   WiFiS3 는 UNO R4 보드 패키지에 포함되어 있어 따로 설치하지 않는다.
 *
 * 설정은 arduino_secrets.h 에 있다. README.md 참고.
 *
 * 배선 주의: R4 의 디지털 핀은 8mA 정도만 흘릴 수 있다. 진동 모터를 핀에 직결하면
 * 핀이 죽는다. MOSFET/트랜지스터 모듈을 거치고 모터 양단에 플라이백 다이오드를 둘 것.
 * (D7 -> 게이트/베이스, 모터 전원은 5V 별도)
 */

#include <WiFiS3.h>
#include <ArduinoMqttClient.h>
#include <ArduinoJson.h>

// WiFi 자격증명은 저장소에 올리지 않는다.
// arduino_secrets.h.example 을 복사해 arduino_secrets.h 를 만들고 값을 채울 것.
// (.gitignore 에 arduino_secrets.h 가 들어 있다)
#include "arduino_secrets.h"

// ---------------------------------------------------------------- 설정

const char WIFI_SSID[]     = SECRET_WIFI_SSID;
const char WIFI_PASS[]     = SECRET_WIFI_PASS;   // 개방망이면 빈 문자열

const char MQTT_HOST[]     = "172.30.6.222";
const int  MQTT_PORT       = 1883;
const char MQTT_TOPIC[]    = "safe-vibe/alert";  // config.py 의 MQTT_TOPIC 과 같아야 한다
// 클라이언트 ID 는 보드 MAC 으로 자동 생성한다. 비워두면 "safe-vibe-<MAC>" 이 되고,
// 값을 적으면 그 값을 그대로 쓴다. 보드를 여러 대 붙일 때 ID 가 겹치면 브로커가
// 먼저 붙은 쪽을 끊어버려 서로 무한 재접속을 반복한다.
const char MQTT_CLIENT_ID[]= "";
const char MQTT_USER[]     = "";                 // 인증을 걸면 채운다
const char MQTT_PASSWORD[] = "";

const int  MOTOR_PIN       = 7;

// 진동 패턴: PULSE_COUNT 번, ON/OFF 를 반복한다. delay() 를 쓰지 않는다.
const int          PULSE_COUNT  = 3;
const unsigned long PULSE_ON_MS  = 300;
const unsigned long PULSE_OFF_MS = 150;

// QoS 1 은 "적어도 한 번"이라 같은 이벤트가 두 번 올 수 있다. 서버도 track_id 로
// 걸러내라고 안내한다. 같은 (track_id, roi_id) 가 이 시간 안에 또 오면 무시한다.
const unsigned long DEDUP_MS = 3000;

const unsigned long RECONNECT_MS = 3000;   // MQTT 재접속 간격

// ---------------------------------------------------------------- 상태

WiFiClient  net;
MqttClient  mqtt(net);

// 진동 상태 머신 — loop() 를 막지 않기 위한 것. 모터가 도는 동안에도
// mqtt.poll() 이 계속 돌아야 keepalive 가 유지되고 다음 이벤트를 놓치지 않는다.
int           pulsesLeft   = 0;
bool          motorOn      = false;
unsigned long phaseStarted = 0;

unsigned long lastReconnectAttempt = 0;

// MQTT 3.1.1 이 모든 브로커에 보장하는 클라이언트 ID 길이는 23자다.
// "safe-vibe-" + MAC 12자 = 22자라 안전하게 들어간다.
char clientId[24];

// 최근 이벤트 기억용 (중복 제거)
const int DEDUP_SLOTS = 8;
struct Seen { long trackId; long roiId; unsigned long at; };
Seen seen[DEDUP_SLOTS];
int  seenNext = 0;

// ---------------------------------------------------------------- 진동

void startVibration() {
  pulsesLeft   = PULSE_COUNT;
  motorOn      = true;
  phaseStarted = millis();
  digitalWrite(MOTOR_PIN, HIGH);
}

void serviceVibration() {
  if (pulsesLeft <= 0) return;

  unsigned long elapsed = millis() - phaseStarted;

  if (motorOn && elapsed >= PULSE_ON_MS) {
    digitalWrite(MOTOR_PIN, LOW);
    motorOn      = false;
    phaseStarted = millis();
    pulsesLeft--;
    if (pulsesLeft <= 0) {
      digitalWrite(LED_BUILTIN, LOW);
    }
  } else if (!motorOn && elapsed >= PULSE_OFF_MS && pulsesLeft > 0) {
    digitalWrite(MOTOR_PIN, HIGH);
    motorOn      = true;
    phaseStarted = millis();
  }
}

// ---------------------------------------------------------------- 중복 제거

bool isDuplicate(long trackId, long roiId) {
  unsigned long now = millis();
  for (int i = 0; i < DEDUP_SLOTS; i++) {
    if (seen[i].at == 0) continue;
    if (seen[i].trackId == trackId && seen[i].roiId == roiId
        && (now - seen[i].at) < DEDUP_MS) {
      return true;
    }
  }
  seen[seenNext] = { trackId, roiId, now };
  seenNext = (seenNext + 1) % DEDUP_SLOTS;
  return false;
}

// ---------------------------------------------------------------- MQTT 수신

void onMqttMessage(int messageSize) {
  // 페이로드를 통째로 읽는다. roi_name 이 한글이면 UTF-8 이라 바이트가 늘어난다.
  char buf[512];
  int n = 0;
  while (mqtt.available() && n < (int)sizeof(buf) - 1) {
    buf[n++] = (char)mqtt.read();
  }
  buf[n] = '\0';

  // 512 바이트를 넘겨 잘린 JSON 은 파싱이 실패한다. 남은 바이트는 비워야
  // 다음 메시지가 앞 메시지 꼬리부터 읽히지 않는다.
  while (mqtt.available()) mqtt.read();

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, buf, n);
  if (err) {
    Serial.print("[mqtt] JSON 파싱 실패: ");
    Serial.println(err.c_str());
    return;
  }

  const char* event = doc["event"] | "";
  if (strcmp(event, "roi_dwell") != 0) {
    Serial.print("[mqtt] 알 수 없는 event, 무시: ");
    Serial.println(event);
    return;
  }

  long        trackId = doc["track_id"] | -1L;
  long        roiId   = doc["roi_id"]   | -1L;
  const char* roiName = doc["roi_name"] | "?";
  const char* cls     = doc["name"]     | "?";
  float       dwell   = doc["dwell"]    | 0.0f;

  if (isDuplicate(trackId, roiId)) {
    Serial.print("[event] 중복 무시 — #");
    Serial.println(trackId);
    return;
  }

  Serial.print("[event] ");
  Serial.print(roiName);
  Serial.print(" — #");
  Serial.print(trackId);
  Serial.print(' ');
  Serial.print(cls);
  Serial.print(' ');
  Serial.print(dwell, 1);
  Serial.println("초 체류 -> 진동");

  digitalWrite(LED_BUILTIN, HIGH);
  startVibration();
}

// ---------------------------------------------------------------- 연결

void buildClientId() {
  if (strlen(MQTT_CLIENT_ID) > 0) {          // 직접 지정했으면 그대로 쓴다
    strncpy(clientId, MQTT_CLIENT_ID, sizeof(clientId) - 1);
    clientId[sizeof(clientId) - 1] = '\0';
    return;
  }

  // MAC 은 WiFi 접속 전에도 읽힌다(모듈 초기화만 되면 된다).
  // WiFiS3 는 배열을 역순으로 채우므로 5 -> 0 으로 읽어야 표기 순서와 같아진다.
  byte mac[6];
  WiFi.macAddress(mac);
  snprintf(clientId, sizeof(clientId), "safe-vibe-%02X%02X%02X%02X%02X%02X",
           mac[5], mac[4], mac[3], mac[2], mac[1], mac[0]);
}

void ensureWiFi() {
  if (WiFi.status() == WL_CONNECTED) return;

  Serial.print("[wifi] 접속 시도: ");
  Serial.println(WIFI_SSID);

  // 개방망(비밀번호 없음)과 WPA 를 구분해서 호출한다.
  if (strlen(WIFI_PASS) == 0) {
    WiFi.begin(WIFI_SSID);
  } else {
    WiFi.begin(WIFI_SSID, WIFI_PASS);
  }

  unsigned long started = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - started < 15000) {
    delay(250);
    Serial.print('.');
  }
  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("[wifi] 연결됨 — IP ");
    Serial.print(WiFi.localIP());
    Serial.print("  RSSI ");
    Serial.println(WiFi.RSSI());
  } else {
    Serial.println("[wifi] 연결 실패 — 다시 시도합니다");
  }
}

bool connectMqtt() {
  Serial.print("[mqtt] ");
  Serial.print(MQTT_HOST);
  Serial.print(':');
  Serial.print(MQTT_PORT);
  Serial.println(" 접속 시도");

  mqtt.setId(clientId);
  if (strlen(MQTT_USER) > 0) {
    mqtt.setUsernamePassword(MQTT_USER, MQTT_PASSWORD);
  }
  mqtt.setKeepAliveInterval(30 * 1000);

  if (!mqtt.connect(MQTT_HOST, MQTT_PORT)) {
    Serial.print("[mqtt] 접속 실패, error = ");
    Serial.println(mqtt.connectError());
    return false;
  }

  mqtt.onMessage(onMqttMessage);
  mqtt.subscribe(MQTT_TOPIC, 1);      // QoS 1
  Serial.print("[mqtt] 연결됨 — 구독: ");
  Serial.println(MQTT_TOPIC);
  return true;
}

// ---------------------------------------------------------------- 엔트리

void setup() {
  pinMode(MOTOR_PIN, OUTPUT);
  digitalWrite(MOTOR_PIN, LOW);
  pinMode(LED_BUILTIN, OUTPUT);
  digitalWrite(LED_BUILTIN, LOW);

  Serial.begin(115200);
  unsigned long t0 = millis();
  while (!Serial && millis() - t0 < 2000) { }   // 시리얼 모니터 없이도 진행

  Serial.println();
  Serial.println("=== safe-vibe-client (UNO R4 WiFi) ===");

  if (WiFi.status() == WL_NO_MODULE) {
    Serial.println("[wifi] 모듈을 찾을 수 없습니다. 보드 선택을 확인하세요.");
    while (true) { delay(1000); }
  }

  buildClientId();
  Serial.print("[mqtt] client id : ");
  Serial.println(clientId);

  ensureWiFi();
}

void loop() {
  ensureWiFi();

  if (WiFi.status() == WL_CONNECTED && !mqtt.connected()) {
    if (millis() - lastReconnectAttempt >= RECONNECT_MS) {
      lastReconnectAttempt = millis();
      connectMqtt();
    }
  }

  // 수신 콜백은 여기서 불린다. 절대 막으면 안 되는 자리.
  mqtt.poll();

  serviceVibration();
}
