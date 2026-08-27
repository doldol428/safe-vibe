"""safe_video1.mp4 -> MJPEG 스트리밍 + ROI(검출 영역) 관리. 표준 라이브러리만 사용.

ROI 모델은 vunexai-frontend 와 동일하게 "이름 붙은 폴리곤 목록"이다.
좌표는 0~1 정규화라 해상도가 바뀌어도 그대로 쓸 수 있고, 저장소는 roi.json 한 개다.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent

VIDEO = Path(os.environ.get("VIDEO", HERE / "safe_video1.mp4"))
INDEX_FILE = HERE / "index.html"
ROI_FILE = Path(os.environ.get("ROI_FILE", HERE / "roi.json"))
PORT = int(os.environ.get("PORT", "8080"))
FPS = os.environ.get("FPS", "15")
QUALITY = os.environ.get("QUALITY", "5")   # ffmpeg -q:v: 2(최상) ~ 31(최하)
WIDTH = os.environ.get("WIDTH", "")        # 예: "1280" 으로 축소, 빈 값이면 원본

# ffmpeg의 mpjpeg 머서가 multipart 경계를 직접 만들어주므로 파이썬은 바이트만 중계한다.
BOUNDARY = "ffmpeg"

MAX_ROI = 10        # vunexai-frontend 의 MAX_ROI_COUNT 와 동일
MAX_POINTS = 30
MAX_NAME = 50
DEFAULT_NAME = "New Zone"

_lock = threading.Lock()


# ---------------------------------------------------------------- ROI 파일 I/O

def _clean_points(raw):
    """좌표 배열을 0~1 범위로 정규화한다. 형식이 틀린 점은 버린다."""
    points = []
    for p in raw if isinstance(raw, list) else []:
        try:
            x, y = float(p["x"]), float(p["y"])
        except (TypeError, KeyError, ValueError):
            continue
        points.append({"x": round(min(max(x, 0.0), 1.0), 6),
                       "y": round(min(max(y, 0.0), 1.0), 6)})
        if len(points) >= MAX_POINTS:
            break
    return points


def _clean_roi(raw, roi_id):
    name = str(raw.get("name") or DEFAULT_NAME)[:MAX_NAME]
    return {
        "id": roi_id,
        "name": name,
        "enabled": bool(raw.get("enabled", True)),
        "points": _clean_points(raw.get("points")),
    }


def load_store():
    """파일이 없거나 깨졌으면 빈 목록으로 시작한다 (예외 없음)."""
    try:
        raw = json.loads(ROI_FILE.read_text(encoding="utf-8"))
        rois = [_clean_roi(r, int(r["id"])) for r in raw["rois"]][:MAX_ROI]
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError,
            KeyError, TypeError, ValueError):
        return {"rois": [], "next_id": 1}
    next_id = max([r["id"] for r in rois], default=0) + 1
    return {"rois": rois, "next_id": max(int(raw.get("next_id", 1)), next_id)}


def save_store(store):
    """임시 파일에 쓴 뒤 원자적으로 교체 — 중간에 죽어도 반쪽 파일이 남지 않는다."""
    tmp = ROI_FILE.with_name(ROI_FILE.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(ROI_FILE)   # 같은 디렉터리 내 교체는 Windows/POSIX 모두 원자적


# ---------------------------------------------------------------- ROI CRUD

def list_rois():
    return load_store()["rois"]


def create_roi(body):
    with _lock:
        store = load_store()
        if len(store["rois"]) >= MAX_ROI:
            return None, f"ROI는 최대 {MAX_ROI}개까지입니다"
        roi = _clean_roi(body, store["next_id"])
        store["rois"].append(roi)
        store["next_id"] += 1
        save_store(store)
        return roi, None


def update_roi(roi_id, body):
    """전달된 필드만 갱신한다 (부분 수정)."""
    with _lock:
        store = load_store()
        for i, roi in enumerate(store["rois"]):
            if roi["id"] != roi_id:
                continue
            merged = {**roi, **{k: body[k] for k in ("name", "enabled", "points")
                                if k in body}}
            store["rois"][i] = _clean_roi(merged, roi_id)
            save_store(store)
            return store["rois"][i], None
        return None, "해당 ROI 없음"


def delete_roi(roi_id):
    with _lock:
        store = load_store()
        remaining = [r for r in store["rois"] if r["id"] != roi_id]
        if len(remaining) == len(store["rois"]):
            return False
        store["rois"] = remaining
        save_store(store)
        return True


# ---------------------------------------------------------------- ffmpeg

def ffmpeg_cmd():
    # ROI는 브라우저 캔버스에 오버레이로 그리므로 스트림 자체는 손대지 않는다.
    # 덕분에 ROI를 편집해도 ffmpeg를 다시 띄울 필요가 없다.
    vf = ["-vf", f"scale={WIDTH}:-2"] if WIDTH else []
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-stream_loop", "-1",   # 무한 반복
        "-re",                  # 실시간 속도로 재생
        "-i", str(VIDEO),
        "-an",
        "-r", FPS,
        *vf,
        "-q:v", QUALITY,
        "-f", "mpjpeg", "pipe:1",
    ]


# ---------------------------------------------------------------- HTTP

ROI_ID_PATH = re.compile(r"^/api/rois/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    disable_nagle_algorithm = True   # 작은 JSON 응답이 지연되지 않도록 TCP_NODELAY

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def do_GET(self):
        if self.path.startswith("/stream.mjpg"):
            self.stream()
        elif self.path == "/api/rois":
            self._json({"rois": list_rois(), "max": MAX_ROI})
        elif self.path in ("/", "/index.html"):
            try:
                body = INDEX_FILE.read_bytes()
            except FileNotFoundError:
                return self.send_error(500, "index.html not found")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/rois":
            return self.send_error(404)
        try:
            roi, err = create_roi(self._body())
        except (ValueError, json.JSONDecodeError):
            return self.send_error(400, "invalid json")
        self._json({"error": err} if err else roi, 409 if err else 201)

    def do_PUT(self):
        m = ROI_ID_PATH.match(self.path)
        if not m:
            return self.send_error(404)
        try:
            roi, err = update_roi(int(m.group(1)), self._body())
        except (ValueError, json.JSONDecodeError):
            return self.send_error(400, "invalid json")
        self._json({"error": err} if err else roi, 404 if err else 200)

    def do_DELETE(self):
        m = ROI_ID_PATH.match(self.path)
        if not m:
            return self.send_error(404)
        if not delete_roi(int(m.group(1))):
            return self.send_error(404, "no such roi")
        self._json({"deleted": int(m.group(1))})

    def stream(self):
        proc = subprocess.Popen(ffmpeg_cmd(), stdout=subprocess.PIPE, bufsize=0)
        try:
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type",
                             f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.end_headers()
            while True:
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # 클라이언트가 탭을 닫음
        finally:
            proc.terminate()      # 접속 1개당 ffmpeg 1개, 끊기면 같이 종료
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        raise SystemExit("ffmpeg를 PATH에서 찾을 수 없습니다")
    if not VIDEO.exists():
        raise SystemExit(f"영상 파일 없음: {VIDEO}")

    # Windows에서는 SO_REUSEADDR 때문에 두 번째 인스턴스가 같은 포트에
    # 조용히 바인딩된다. 꺼두면 중복 실행 시 바로 에러가 난다.
    class Server(ThreadingHTTPServer):
        allow_reuse_address = False
        daemon_threads = True

    # Windows에서 localhost는 ::1(IPv6)로 먼저 풀린다. IPv4만 듣고 있으면
    # 클라이언트가 ::1 시도 -> 실패 -> IPv4 폴백을 거치느라 요청마다
    # 수백 ms가 붙는다. 듀얼스택으로 열어 IPv6/IPv4를 한 소켓으로 받는다.
    class DualStackServer(Server):
        address_family = socket.AF_INET6

        def server_bind(self):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            super().server_bind()

    print(f"serving http://localhost:{PORT}  (stream: /stream.mjpg)", flush=True)
    print(f"roi file: {ROI_FILE}  ->  {len(list_rois())} ROI", flush=True)
    try:
        # OSError를 잡아서 폴백하면 "포트 사용 중" 에러까지 삼켜버리므로,
        # IPv6 지원 여부는 미리 물어보고 고른다.
        httpd = (DualStackServer(("::", PORT), Handler)
                 if socket.has_dualstack_ipv6()
                 else Server(("0.0.0.0", PORT), Handler))
        httpd.serve_forever()
    except OSError as e:
        raise SystemExit(f"포트 {PORT} 바인딩 실패 (이미 실행 중?): {e}")
    except KeyboardInterrupt:
        pass
