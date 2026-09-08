# safe-vibe

카메라/영상에서 사람을 검출·추적하고, 지정한 구역(ROI)에 일정 시간 이상 머무르면
경보를 내는 데모. 라즈베리파이 5 CPU만으로 돌아간다.

```
FrameSource (Picamera2 / ffmpeg)
        │
     BGR Frame ─┬─ ONNX 추론 → IoU 추적 → ROI 체류 판정 → 이벤트 ─→ MQTT ─→ 진동 알림(UNO R4)
                └─ JPEG 인코딩 → MJPEG ─────────────────────────────→ 브라우저
```

AI는 원본 프레임을 그대로 쓰고 송출용 JPEG는 따로 만든다. 추론 FPS(`AI_FPS`)와
송출 FPS(`STREAM_FPS`)가 분리돼 있어 화면은 부드럽고 CPU는 추론에만 쓰인다.

## 실행

```bash
python setup.py
```

venv 생성 → 패키지 설치 → 샘플 영상 정리 → yolov8n ONNX 준비 → ffmpeg 설치까지
한 번에 한다. 전부 멱등이라 다시 돌려도 끝난 단계는 건너뛴다. (`--check` 로 상태만 점검)

```bash
.venv/bin/python app.py          # Windows: .venv\Scripts\python app.py
```

브라우저에서 http://localhost:8080 — 영상 위에 폴리곤을 그려 ROI를 만들고,
검출 박스·체류 이벤트를 실시간으로 본다.

## 구성

| 파일                  | 역할                                                                            |
| --------------------- | ------------------------------------------------------------------------------- |
| `app.py`            | HTTP 서버, 추론 워커, ROI 체류 판정과 이벤트 발행                               |
| `frames.py`         | 프레임 소스(Picamera2/ffmpeg)와 공유 파이프라인, MJPEG fan-out                  |
| `detector.py`       | `model/` 의 단일 ONNX를 onnxruntime으로 서빙 (클래스명은 메타데이터에서 읽음) |
| `tracker.py`        | IoU 기반 트래커 — 같은 사람에게 ID를 유지해 중복 경보를 막는다                 |
| `roistore.py`       | ROI CRUD,`roi.json` 저장 (좌표는 0~1 정규화)                                  |
| `mqttpub.py`        | 이벤트 MQTT 발행 (브로커가 없어도 앱은 그대로 동작)                             |
| `index.html`        | 단일 파일 웹 UI                                                                 |
| `safe-vibe-client/` | 경보를 받아 진동으로 알리는 Arduino UNO R4 WiFi 클라이언트                      |

`video/` 에 mp4 하나, `model/` 에 onnx 하나 — 디렉터리마다 파일 하나가 규칙이다.

## 설정

모든 값은 `config.py` 에 모여 있고 같은 이름의 환경변수로 덮어쓴다.

```bash
STREAM_W=640 AI_FPS=2 CONF_THRESHOLD=0.45 python app.py
```

자주 건드리는 것들:

| 변수                            | 기본값           | 설명                                                         |
| ------------------------------- | ---------------- | ------------------------------------------------------------ |
| `SOURCE`                      | `auto`         | `picamera` / `video` / `auto`(picamera2 있으면 카메라) |
| `AI_FPS`                      | `4`            | 추론 주기. Pi 5 CPU 기준 3~5 권장                            |
| `STREAM_W` / `JPEG_QUALITY` | `960` / `75` | 송출 대역폭 조절                                             |
| `ROI_MATCH`                   | `overlap`      | `overlap`(박스 겹침 비율) / `foot`(발밑 점이 폴리곤 안)  |
| `DWELL_SEC` / `EXIT_SEC`    | `2` / `2`    | 이벤트를 낼 체류 시간 / 이탈 인정 시간                       |
| `MQTT_HOST`                   | `127.0.0.1`    | 빈 값이면 MQTT 연동을 끈다                                   |

## API

| 엔드포인트                                            | 설명                             |
| ----------------------------------------------------- | -------------------------------- |
| `GET /stream.mjpg`                                  | MJPEG 스트림                     |
| `GET /api/detections`                               | 현재 트랙, ROI별 인원, 추론 지연 |
| `GET /api/events`                                   | 최근 체류 이벤트                 |
| `GET /api/status`                                   | 소스·모델·트래커 설정 요약     |
| `GET/POST /api/rois`, `PUT/DELETE /api/rois/{id}` | ROI 관리                         |

## 라즈베리파이

numpy/opencv/picamera2 는 pip 빌드가 오래 걸려 apt 쪽을 쓴다. `setup.py` 가
ARM 리눅스를 감지해 `--system-site-packages` 로 venv를 만든다.

```bash
sudo apt install -y python3-picamera2 python3-opencv python3-numpy
python setup.py
```

`SOURCE=auto` 는 카메라가 **실제로 연결돼 있을 때만** 카메라를 쓰고, 없으면 영상
파일로 넘어간다. 카메라를 꽂았는데도 영상 파일로 가면 `python setup.py --check`
의 카메라 줄을 확인한다.

---

## 만든 의도

현장 안전 감시(위험 구역 체류, 테일게이팅 등)를 **GPU 없이 라즈베리파이 한 대로
어디까지 할 수 있는지** 확인해 보려고 만든 데모다. 세 가지에 답하는 것이 목표였다.

1. CPU만으로 실시간 감시가 되는가 — 검출 속도가 아니라 *체감 지연*이 쓸 만한가
2. 비개발자가 화면에서 감시 구역을 직접 그릴 수 있는가
3. 감지한 이벤트를 화면 밖(다른 장치)까지 전달할 수 있는가

그래서 의도적으로 **뺀 것**들이 있다. GPU, 모델 서버(Triton), 데이터베이스,
웹 프레임워크(Flask/FastAPI), 프론트엔드 빌드 도구. 의존성은 `onnxruntime`,
`opencv-headless`, `numpy`, `paho-mqtt` 넷뿐이고 나머지는 표준 라이브러리다.
읽는 사람이 처음부터 끝까지 따라갈 수 있는 크기를 유지하는 쪽을 택했다.

핵심 설계 결정은 다음과 같다.

| 결정                                | 이유                                                                                                              |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| 추론과 송출을 분리                  | MJPEG을 다시 디코드해 추론하는 낭비를 없앤다. 화면 15fps / 추론 4fps처럼 따로 조절할 수 있다                      |
| 검출만 쓰지 않고 추적까지           | 프레임 단위 검출만으로는 "같은 사람"을 알 수 없어 경보가 매 프레임 중복된다. 트랙 ID가 있어야 1인 1회 경보가 된다 |
| 체류 시간(`DWELL_SEC`) 판정       | 스쳐 지나가는 사람과 실제로 머무는 사람을 구분한다                                                                |
| 이탈에 유예(`EXIT_SEC`)           | ROI 경계에서 박스가 흔들리면 진입/이탈이 반복돼 같은 사람이 여러 번 잡힌다                                        |
| 좌표는 전부 0~1 정규화              | 캡처·송출·모델 입력 해상도가 제각각이어도 ROI와 검출 박스를 그대로 대조할 수 있다                               |
| 단조 시계(`time.monotonic`)       | Pi는 RTC가 없어 부팅 직후 NTP 동기화로 벽시계가 점프한다. 체류 시간이 틀어진다                                    |
| 클래스명을 ONNX 메타데이터에서 읽음 | 안전모 모델 등으로 교체할 때 코드를 고칠 필요가 없다                                                              |
| 없으면 없는 대로 동작               | 모델이 없으면 검출만 끄고 스트리밍은 계속한다. MQTT도 마찬가지 — 브로커가 죽어도 감시는 멈추지 않는다            |
| DB 없이`roi.json`                 | ROI는 수십 개 수준이고 쓰기는 사람이 편집할 때뿐이다. 파일 하나가 백업·이관·수동편집 모두 쉽다                  |
| 디렉터리마다 파일 하나 규칙         | 어떤 모델/영상이 로드됐는지 고민할 필요가 없다. 설정 항목 하나가 줄어든다                                         |

## 개발 가이드

### 모듈 관계

```
config.py ← 모든 모듈이 참조 (설정은 여기 한 곳에만)
    ↑
frames.py   detector.py   tracker.py   roistore.py   mqttpub.py
    └───────────┴─────────────┴────────────┴─────────────┘
                          app.py  (조립 + HTTP)
```

`app.py` 만 다른 모듈을 알고, 나머지는 서로를 모른다. 새 기능은 대개 해당
모듈에 넣고 `app.py` 에서 연결하는 형태가 된다.

### 프레임 한 장의 LifeCycle

```
Pipeline._loop (캡처 스레드)
  source.read() → BGR 프레임
    ├→ self._frame 에 보관 (+seq 증가)          ← DetectionWorker 가 가져감
    └→ 축소 → JPEG 인코딩 → FrameHub.publish()  → 접속자 전원에게 fan-out

DetectionWorker._loop (추론 스레드, AI_FPS 주기)
  pipeline.latest() → seq 가 바뀌었을 때만 추론
    → detector.infer()   letterbox → ONNX → NMS → 0~1 정규화 박스
    → tracker.update()   IoU greedy 매칭 → 트랙 ID 부여
    → _match_rois()      ROI 겹침 → 체류 시간 누적 → 이벤트
    → mqttpub.publish()
```

### 스레드와 락

스레드는 셋이다. 캡처 1개, 추론 1개, HTTP 요청당 1개(`ThreadingHTTPServer`).

| 락                       | 보호 대상                                  |
| ------------------------ | ------------------------------------------ |
| `Pipeline._lock`       | 최신 raw 프레임과 seq                      |
| `DetectionWorker.lock` | `detections` / `roi_hits` / `events` |
| `roistore._lock`       | `roi.json` 읽기·쓰기                    |

규칙 하나: **락을 쥔 채로 느려질 수 있는 일을 하지 않는다.** MQTT 발행과 로그
출력이 락 밖에 있는 이유가 이것이다. 브로커가 느릴 때 그 지연이 락을 붙들면
`/api/detections` 응답까지 같이 밀린다.

### 자주 하는 작업

**모델 교체** — `model/` 에 onnx 하나만 두면 끝이다. 클래스명은 메타데이터에서
읽으므로 코드 수정은 없고, 감시 대상 클래스만 `EVENT_CLASSES` 로 바꾼다.
단 `detector.py` 는 YOLOv8 detect 출력 형식(`(1, 4+nc, N)` 또는 그 전치)을
가정하므로, 다른 계열 모델은 `_as_rows()` / `infer()` 의 후처리를 손봐야 한다.

**설정 추가** — `config.py` 에 `_int` / `_float` / `_list` 헬퍼로 한 줄 추가하면
같은 이름의 환경변수로 덮어쓸 수 있게 된다. 상수를 다른 파일에 흩뿌리지 않는다.

**ROI 판정 방식 변경** — `DetectionWorker._in_roi()` 하나만 본다.
겹침 비율(`overlap`)과 발밑 점(`foot`)이 여기서 갈린다.

**새 이벤트 종류 추가** — `_match_rois()` 가 이벤트를 만드는 유일한 곳이다.
`Track` 에 상태 필드를 더하고(예: `roi_since` 처럼) 여기서 조건을 판정한 뒤,
`self.events` 와 `publisher.publish()` 로 내보내면 화면과 MQTT 양쪽에 동시에 실린다.
발행 payload에 필드를 더하면 `safe-vibe-client` 쪽도 같이 확인할 것.

**새 프레임 소스 추가** — `frames.FrameSource` 를 상속해 `read()` 가
BGR `ndarray` 를 돌려주게 만들고 `open_source()` 에 분기를 추가한다.
윗단(`Pipeline` 이상)은 손대지 않는다.

**API 추가** — `Handler.do_GET` 의 분기에 한 줄 넣고 `self._json(...)` 을 부른다.
UI 쪽은 `index.html` 의 `poll()` / `pollEvents()` 가 주기적으로 가져가는 구조다.

**UI 수정** — `index.html` 파일 하나에 다 들어 있다(빌드 없음). 캔버스를 영상 위에
겹쳐 폴리곤을 그리고, 좌표는 `toNorm()` 으로 0~1로 바꿔 서버에 보낸다.

### 성능 조절

Pi 5 CPU 기준으로 추론 3~5fps, 화면 10~15fps가 현실적인 균형점이다.
느리면 순서대로 건드린다.

1. `AI_FPS` 를 낮춘다 — 체감 지연에 가장 크게 영향을 준다
2. `STREAM_W` / `JPEG_QUALITY` 를 낮춘다 — 대역폭과 인코딩 부하가 함께 준다
3. 더 작은 모델(yolov8n)로 바꾼다
4. `ORT_THREADS` 로 추론 스레드 수를 제한한다 — 캡처 스레드와 코어를 두고
   싸우는 상황이면 오히려 빨라진다

현재 처리량은 `/api/status` 와 `/api/detections`(`fps`, `ai_fps`, `infer_ms`)에서
바로 확인할 수 있다.

### 코드 스타일

주석은 **무엇을 하는지가 아니라 왜 그렇게 했는지**를 적는다. 기존 주석이 대부분
"이렇게 안 하면 무엇이 깨지는가"를 설명하는 형태이니 같은 결을 유지하면 된다.

자동화된 테스트는 없다. 변경 후에는 최소한 이 정도를 눈으로 확인한다.

- `python setup.py --check` — 환경이 그대로인지
- 앱 실행 후 브라우저에서 스트림이 뜨고 검출 박스가 그려지는지
- ROI를 그리고 그 안에 `DWELL_SEC` 이상 머물렀을 때 이벤트가 한 번만 뜨는지
- 콘솔에 `[event]` 가 찍히고, 브로커가 있으면 `[mqtt] 연결됨` 이후 구독 쪽에 도달하는지

## 라이선스

MIT ([LICENSE](LICENSE)). 모델 가중치의 라이선스는 [model/NOTICE.md](model/NOTICE.md) 참고.
