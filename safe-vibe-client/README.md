# safe-vibe-client

`safe-vibe` 가 내보내는 ROI 체류 경보를 MQTT 로 받아 진동으로 알리는 Arduino 클라이언트.

```
app.py --publish--> mosquitto --subscribe--> UNO R4 WiFi --> 진동 모터(D7)
```

## 하드웨어

- Arduino UNO R4 WiFi
- 진동 모터 + 드라이버(MOSFET/트랜지스터 모듈), 신호선 **D7**

R4 의 디지털 핀은 8mA 정도가 한계다. 모터를 핀에 직결하면 핀이 죽는다.
드라이버 모듈을 거치고, 모터 양단에 플라이백 다이오드를 둘 것.

## 라이브러리

라이브러리 매니저(`Ctrl+Shift+I`)에서 설치한다.

- `ArduinoMqttClient` (Arduino 공식)
- `ArduinoJson` (Benoit Blanchon, v7)

`WiFiS3` 는 UNO R4 보드 패키지에 포함되어 있어 따로 설치하지 않는다.

## 설정

WiFi 자격증명만 저장소 밖에 둔다.

```
cp arduino_secrets.h.example arduino_secrets.h
```

그리고 SSID / 비밀번호를 채운다. **UNO R4 WiFi 는 2.4GHz 만 지원한다** —
5GHz 전용 SSID 로는 결합 자체가 되지 않는다.

브로커 주소와 토픽은 `safe-vibe-client.ino` 상단에 있다.

| 상수 | 기본값 | 비고 |
| --- | --- | --- |
| `MQTT_HOST` | `172.30.6.183` | 브로커가 뜬 장비의 IP |
| `MQTT_PORT` | `1883` | |
| `MQTT_TOPIC` | `safe-vibe/alert` | `config.py` 의 `MQTT_TOPIC` 과 같아야 한다 |
| `MQTT_CLIENT_ID` | `""` | 비우면 `safe-vibe-<MAC>` 자동 생성 |
| `MOTOR_PIN` | `7` | |
| `PULSE_COUNT` / `PULSE_ON_MS` / `PULSE_OFF_MS` | 3 / 300 / 150 | 진동 패턴 |
| `DEDUP_MS` | `3000` | 같은 `(track_id, roi_id)` 재수신 무시 구간 |

## 수신 페이로드

토픽 `safe-vibe/alert`, QoS 1, retain 없음. `mqttpub.py` 가 보내는 형식:

```json
{"ts":"14:03:22","roi_id":1,"roi_name":"입구","track_id":7,
 "name":"person","dwell":2.1,"event":"roi_dwell","ts_epoch":1788504449.5}
```

`event` 가 `roi_dwell` 인 것만 처리한다.

QoS 1 은 "적어도 한 번"이라 같은 경보가 두 번 올 수 있다. 그래서 최근
`(track_id, roi_id)` 를 8칸 기억해 `DEDUP_MS` 안의 재수신은 버린다.

## 브로커 쪽 준비

mosquitto 는 기본 설정에서 루프백에만 바인딩되는 경우가 있다. 그러면 방화벽과
무관하게 보드가 붙지 못하고 `[mqtt] 접속 실패, error = -2` 만 반복된다.
`mosquitto.conf` 에 아래가 있어야 한다.

```
listener 1883 0.0.0.0
allow_anonymous true
```

`netstat -ano | findstr ":1883"` 에 `0.0.0.0:1883 ... LISTENING` 이 보이면 정상.

주의 두 가지:

- mosquitto 2.x 는 `listener` 를 명시하는 순간 `allow_anonymous` 가 기본
  `false` 가 된다. 위처럼 같이 적지 않으면 error 5(not authorized) 가 난다.
- `listener 1883 0.0.0.0` 은 IPv4 만 듣는다. 같은 장비의 `app.py` 는
  `MQTT_HOST=127.0.0.1` 로 주는 편이 안전하다. `localhost` 가 `::1` 로 먼저
  풀리면 연결되지 않는다.

Windows 방화벽에서 1883 인바운드도 열어야 하며, 규칙의 프로필이 현재 네트워크
프로필(공용/개인)과 일치해야 적용된다.

## 동작 확인

```
mosquitto_pub -h 172.30.6.183 -t safe-vibe/alert -q 1 \
  -m '{"event":"roi_dwell","roi_id":1,"roi_name":"test","track_id":99,"name":"person","dwell":2.1}'
```

`track_id` 를 매번 바꿔서 쏜다. 같은 값으로 연속 발행하면 중복 필터에 걸린다.

시리얼 모니터는 **115200**.

```
=== safe-vibe-client (UNO R4 WiFi) ===
[mqtt] client id : safe-vibe-A1B2C3D4E5F6
[wifi] 연결됨 — IP 172.30.6.201  RSSI -52
[mqtt] 172.30.6.183:1883 접속 시도
[mqtt] 연결됨 — 구독: safe-vibe/alert
[event] test — #99 person 2.1초 체류 -> 진동
```

## 문제 해결

| 증상 | 원인 |
| --- | --- |
| `[wifi] 연결 실패` 반복 | 5GHz SSID, WPA3/Enterprise, 또는 펌웨어 구버전 |
| `error = -2` | TCP 연결 실패 — 브로커 바인딩/방화벽/다른 서브넷 |
| `error = 5` | `allow_anonymous false` 인데 계정 미설정 |
| 연결은 되는데 이벤트 없음 | `app.py` 의 `MQTT_HOST` 가 아직 `localhost`, 또는 토픽 불일치 |
| 두 보드가 번갈아 끊김 | client id 중복 — `MQTT_CLIENT_ID` 를 비워 MAC 자동 생성에 맡길 것 |
