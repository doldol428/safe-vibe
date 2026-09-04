"""설정 한곳 모음.

모든 값은 같은 이름의 환경변수로 덮어쓸 수 있다. 예)
    STREAM_W=640 AI_FPS=2 CONF_THRESHOLD=0.45 python app.py
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _int(name, default):
    return int(os.environ.get(name, default))


def _float(name, default):
    return float(os.environ.get(name, default))


def _path(name, default):
    return Path(os.environ.get(name) or default)


def _list(name, default):
    return [s.strip() for s in os.environ.get(name, default).split(",") if s.strip()]


# ---------------------------------------------------------------- 경로
VIDEO_DIR = _path("VIDEO_DIR", BASE_DIR / "video")   # 여기 mp4 하나만 둔다
MODEL_DIR = _path("MODEL_DIR", BASE_DIR / "model")   # 여기 onnx 하나만 둔다
ROI_FILE = _path("ROI_FILE", BASE_DIR / "roi.json")
INDEX_FILE = BASE_DIR / "index.html"
# 영상 파일을 직접 지정하면 VIDEO_DIR의 단일 파일 규칙을 건너뛴다.
VIDEO = os.environ.get("VIDEO")

# ---------------------------------------------------------------- 서버
PORT = _int("PORT", 8080)

# ---------------------------------------------------------------- 캡처 / 송출
# "picamera" | "video" | "auto"(picamera2가 있으면 카메라, 없으면 영상 파일)
SOURCE = (os.environ.get("SOURCE") or "auto").lower()
CAPTURE_W = _int("CAPTURE_W", 1280)      # AI가 쓰는 원본 해상도
CAPTURE_H = _int("CAPTURE_H", 720)
STREAM_FPS = _float("STREAM_FPS", 15)
# 송출용 가로 해상도. 브라우저에만 축소본을 보내 대역폭을 줄인다. 0이면 원본 그대로.
# 720p q75는 약 20Mbps, 960 폭은 약 12Mbps.
STREAM_W = _int("STREAM_W", 960)
JPEG_QUALITY = _int("JPEG_QUALITY", 75)

# ---------------------------------------------------------------- 추론
AI_FPS = _float("AI_FPS", 4)             # 송출 FPS와 별개로 추론만의 주기
CONF_THRESHOLD = _float("CONF_THRESHOLD", 0.35)
NMS_IOU = _float("IOU_THRESHOLD", 0.5)   # 검출 후처리 NMS (트래커 IoU와 다름)
ORT_THREADS = _int("ORT_THREADS", 0)     # 0이면 onnxruntime 기본값

# ---------------------------------------------------------------- 트래커
# max_age는 "프레임" 단위라 AI_FPS에 따라 실제 시간이 달라진다.
# AI 4fps 기준 12프레임 = 약 3초. 검출이 끊겨도 이만큼은 트랙을 유지한다.
# 크게 잡으면 ID는 안정되지만 사라진 객체의 트랙이 남아 다른 사람에게
# 잘못 이어붙을 수 있다. 검출이 자주 끊기는 영상일수록 키워야 한다.
TRACK_MAX_AGE = _int("TRACK_MAX_AGE", 12)
TRACK_MIN_HITS = _int("TRACK_MIN_HITS", 2)   # 오검출 억제
TRACK_IOU = _float("TRACK_IOU", 0.3)         # 검출-트랙 매칭 임계값

# ---------------------------------------------------------------- 이벤트
EVENT_CLASSES = _list("EVENT_CLASSES", "person")
# ROI 진입 판정 방식.
#   overlap : 박스와 ROI 가 겹치는 면적이 박스 면적의 ROI_OVERLAP_MIN 을 넘으면 진입.
#             0 이면 조금이라도 겹치면 진입. 정면/내려보는 카메라처럼 발이 안 보이거나
#             ROI 를 사람 몸 주변에 그리는 경우에 맞다.
#   foot    : 박스 하단 중앙(발밑)이 폴리곤 안에 있어야 진입. 바닥에 구역을 그릴 때.
ROI_MATCH = (os.environ.get("ROI_MATCH") or "overlap").lower()
if ROI_MATCH not in ("overlap", "foot"):
    raise SystemExit(f"ROI_MATCH 는 overlap 또는 foot 이어야 합니다: {ROI_MATCH!r}")
ROI_OVERLAP_MIN = _float("ROI_OVERLAP_MIN", 0)   # 0~1. 이 값을 '초과'해야 진입
# ROI 안에 이만큼 계속 머물러야 이벤트를 낸다. 스쳐 지나가는 것과 구분한다.
DWELL_SEC = _float("DWELL_SEC", 2)
# ROI 경계에서 박스가 흔들리면 진입/이탈이 반복돼 같은 사람이 여러 번 잡힌다.
# 이만큼 계속 벗어나 있어야 "나갔다"로 인정한다.
EXIT_SEC = _float("EXIT_SEC", 2)
EVENT_LOG_SIZE = _int("EVENT_LOG_SIZE", 50)  # 메모리에 보관할 최근 이벤트 수

# ---------------------------------------------------------------- MQTT
# 이벤트를 외부로 내보낸다. 기본값은 같은 장비의 브로커(mosquitto)다.
# 브로커가 없어도 앱은 그대로 돈다 — 연결만 계속 재시도하고 이벤트는 버린다.
# 다른 브로커로 보내려면 MQTT_HOST=192.168.0.10, 아예 끄려면 MQTT_HOST= (빈 값).
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = _int("MQTT_PORT", 1883)
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "safe-vibe/alert")
# QoS 1 = "적어도 한 번". 경보는 유실보다 중복이 낫다. 받는 쪽에서 track_id 로
# 중복을 걸러낼 수 있다.
MQTT_QOS = _int("MQTT_QOS", 1)
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "")   # 비우면 브로커가 생성

# ---------------------------------------------------------------- ROI 제약
MAX_ROI = _int("MAX_ROI", 10)        # 한 화면에 둘 수 있는 검출 영역 수
MAX_POINTS = _int("MAX_POINTS", 30)  # 폴리곤 꼭짓점 수 상한
MAX_NAME = _int("MAX_NAME", 50)      # ROI 이름 길이 상한
DEFAULT_ROI_NAME = "New Zone"
