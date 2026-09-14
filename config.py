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
VIDEO_DIR = _path("VIDEO_DIR", BASE_DIR / "video")   # 여러 개면 이름순 첫 번째 mp4
MODEL_DIR = _path("MODEL_DIR", BASE_DIR / "model")   # 여기 onnx 하나만 둔다
ROI_FILE = _path("ROI_FILE", BASE_DIR / "roi.json")
INDEX_FILE = BASE_DIR / "index.html"
ANALYSIS_FILE = BASE_DIR / "analysis.html"
# 영상 파일을 직접 지정하면 VIDEO_DIR 에서 고르지 않고 그 파일을 쓴다.
VIDEO = os.environ.get("VIDEO")

# ---------------------------------------------------------------- 서버
PORT = _int("PORT", 8080)

# ---------------------------------------------------------------- 캡처 / 송출
# "picamera" | "webcam" | "video" | "auto"
#   auto: picamera2 카메라 -> USB 웹캠 -> 영상 파일 순으로, 열리는 첫 번째 것을 쓴다.
SOURCE = (os.environ.get("SOURCE") or "auto").lower()
# USB 웹캠 장치 번호. -1 이면 자동 탐색(리눅스: sysfs 에서 USB 캡처 노드, 그 외 OS: 0).
WEBCAM_INDEX = _int("WEBCAM_INDEX", -1)
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

# ---------------------------------------------------------------- 자세 / 방향 분석
# 박스만으로는 사람이 어느 쪽을 보는지 알 수 없어 YOLOv8-pose 를 검출 모델과 같이 돌린다.
# model/ 의 "onnx 하나" 규칙과 섞이지 않게 하위 디렉터리에 둔다. 파일이 없으면 분석만 꺼진다.
# 사람 트랙이 있는 프레임에서만 돌므로 사람이 없을 때는 CPU 를 더 쓰지 않는다.
POSE_MODEL = _path("POSE_MODEL", BASE_DIR / "model" / "pose" / "yolov8n-pose.onnx")
POSE_CLASS = os.environ.get("POSE_CLASS", "person")   # 검출 모델에서 pose 를 붙일 클래스
POSE_CONF = _float("POSE_CONF", 0.35)
POSE_MATCH_IOU = _float("POSE_MATCH_IOU", 0.3)   # pose 사람 박스 <-> 트랙 매칭 임계값
KP_CONF = _float("KP_CONF", 0.5)                 # 이 신뢰도 이상인 관절점만 방향 판정에 쓴다
# 어깨 x 간격이 박스 폭의 이 비율 이상이면 정면/뒷모습, 미만이면 옆모습.
# 0.25 로는 완전한 옆모습이 뒷모습으로 잡혀서 0.3 으로 둔다.
FACING_SIDE_RATIO = _float("FACING_SIDE_RATIO", 0.3)
# 코가 어깨 중심에서 박스 폭의 이 비율 이상 벗어나면 고개가 그쪽을 향한 것으로 본다.
FACING_HEAD_RATIO = _float("FACING_HEAD_RATIO", 0.08)
# 트랙별 최근 N번 판정의 다수결을 방향으로 쓴다. 한 프레임짜리 오판을 걸러낸다.
FACING_WINDOW = _int("FACING_WINDOW", 5)
FACING_HISTORY = _int("FACING_HISTORY", 40)      # 분석 페이지에 보여줄 트랙별 판정 이력 수

# ---------------------------------------------------------------- 트래커
# max_age는 "프레임" 단위라 AI_FPS에 따라 실제 시간이 달라진다.
# AI 4fps 기준 12프레임 = 약 3초. 검출이 끊겨도 이만큼은 트랙을 유지한다.
# 크게 잡으면 ID는 안정되지만 사라진 객체의 트랙이 남아 다른 사람에게
# 잘못 이어붙을 수 있다. 검출이 자주 끊기는 영상일수록 키워야 한다.
TRACK_MAX_AGE = _int("TRACK_MAX_AGE", 12)
TRACK_MIN_HITS = _int("TRACK_MIN_HITS", 2)   # 오검출 억제
TRACK_IOU = _float("TRACK_IOU", 0.3)         # 검출-트랙 매칭 임계값
TRACK_TRAIL = _int("TRACK_TRAIL", 12)        # 트랙별로 기억할 중심점 이력 수 (낙하 판정용)

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

# ---------------------------------------------------------------- 낙하 경보
# FALL_CLASS 트랙의 중심이 짧은 시간에 아래로 크게 움직이면 떨어지는 것으로 보고,
# 가까운 사람의 왼쪽/오른쪽 진동 클라이언트로 경보를 보낸다 (fall.py).
# 거리/속도는 전부 화면 크기 기준 0~1 이다.
FALL_CLASS = os.environ.get("FALL_CLASS", "box")
FALL_WINDOW_SEC = _float("FALL_WINDOW_SEC", 1.0)    # 이 시간 동안의 움직임으로 판정
# 쌓여 있는 박스도 검출이 흔들려 중심이 0.01 정도 오르내린다. 그보다 확실히 크게 잡는다.
FALL_MIN_DROP = _float("FALL_MIN_DROP", 0.03)       # 누적 하강량 (화면 높이 비율)
FALL_MIN_SPEED = _float("FALL_MIN_SPEED", 0.1)      # 직전 구간 하강 속도 (화면 높이/초)
# 사람 몸 중심에서 사람 박스 폭의 이 배수 안쪽으로 떨어지면 그 사람에게 경보를 낸다.
FALL_NEAR_RATIO = _float("FALL_NEAR_RATIO", 1.5)
# 이 안쪽이면 머리 바로 위로 떨어지는 것이라 좌/우를 가르지 않고 양쪽으로 보낸다.
FALL_CENTER_RATIO = _float("FALL_CENTER_RATIO", 0.25)
# 최근 어깨 간격 평균(박스 폭 대비)이 이보다 좁으면 옆모습이라 몸의 좌/우를 알 수 없어 양쪽.
# 방향 표시(FACING_SIDE_RATIO=0.3)보다 낮게 둔다 — 비스듬한 뒷모습(-0.3~-0.45)은 좌우가 분명하다.
FALL_PROFILE_RATIO = _float("FALL_PROFILE_RATIO", 0.15)
# 떨어지는 동안 박스 검출이 끊겨 새 트랙으로 다시 잡혀도, 같은 사람에게는 이만큼 한 번만 울린다.
FALL_COOLDOWN_SEC = _float("FALL_COOLDOWN_SEC", 3)

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
# ROI 마다 어떤 이벤트로 진동 알림(MQTT 발행)을 낼지. 화면 이벤트 목록에는 설정과 무관하게 남는다.
# 새로 만든 ROI 와, 이 필드가 없던 예전 roi.json 의 ROI 가 이 값으로 시작한다.
#   alert_dwell : 체류/침입. 사람이 지나다니는 구역이면 계속 울려서 기본은 끈다.
#   alert_fall  : 이 ROI 안에 있는 사람에게 떨어지는 낙하. ROI 밖 사람은 설정과 무관하게 울린다.
DEFAULT_ALERT_DWELL = False
DEFAULT_ALERT_FALL = True
