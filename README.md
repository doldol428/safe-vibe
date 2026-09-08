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

| 파일 | 역할 |
| --- | --- |
| `app.py` | HTTP 서버, 추론 워커, ROI 체류 판정과 이벤트 발행 |
| `frames.py` | 프레임 소스(Picamera2/ffmpeg)와 공유 파이프라인, MJPEG fan-out |
| `detector.py` | `model/` 의 단일 ONNX를 onnxruntime으로 서빙 (클래스명은 메타데이터에서 읽음) |
| `tracker.py` | IoU 기반 트래커 — 같은 사람에게 ID를 유지해 중복 경보를 막는다 |
| `roistore.py` | ROI CRUD, `roi.json` 저장 (좌표는 0~1 정규화) |
| `mqttpub.py` | 이벤트 MQTT 발행 (브로커가 없어도 앱은 그대로 동작) |
| `index.html` | 단일 파일 웹 UI |
| `safe-vibe-client/` | 경보를 받아 진동으로 알리는 Arduino UNO R4 WiFi 클라이언트 |

`video/` 에 mp4 하나, `model/` 에 onnx 하나 — 디렉터리마다 파일 하나가 규칙이다.

## 설정

모든 값은 `config.py` 에 모여 있고 같은 이름의 환경변수로 덮어쓴다.

```bash
STREAM_W=640 AI_FPS=2 CONF_THRESHOLD=0.45 python app.py
```

자주 건드리는 것들:

| 변수 | 기본값 | 설명 |
| --- | --- | --- |
| `SOURCE` | `auto` | `picamera` / `video` / `auto`(picamera2 있으면 카메라) |
| `AI_FPS` | `4` | 추론 주기. Pi 5 CPU 기준 3~5 권장 |
| `STREAM_W` / `JPEG_QUALITY` | `960` / `75` | 송출 대역폭 조절 |
| `ROI_MATCH` | `overlap` | `overlap`(박스 겹침 비율) / `foot`(발밑 점이 폴리곤 안) |
| `DWELL_SEC` / `EXIT_SEC` | `2` / `2` | 이벤트를 낼 체류 시간 / 이탈 인정 시간 |
| `MQTT_HOST` | `127.0.0.1` | 빈 값이면 MQTT 연동을 끈다 |

## API

| 엔드포인트 | 설명 |
| --- | --- |
| `GET /stream.mjpg` | MJPEG 스트림 |
| `GET /api/detections` | 현재 트랙, ROI별 인원, 추론 지연 |
| `GET /api/events` | 최근 체류 이벤트 |
| `GET /api/status` | 소스·모델·트래커 설정 요약 |
| `GET/POST /api/rois`, `PUT/DELETE /api/rois/{id}` | ROI 관리 |

## 라즈베리파이

numpy/opencv/picamera2 는 pip 빌드가 오래 걸려 apt 쪽을 쓴다. `setup.py` 가
ARM 리눅스를 감지해 `--system-site-packages` 로 venv를 만든다.

```bash
sudo apt install -y python3-picamera2 python3-opencv python3-numpy
python setup.py
```

## 라이선스

MIT ([LICENSE](LICENSE)). 모델 가중치의 라이선스는 [model/NOTICE.md](model/NOTICE.md) 참고.
