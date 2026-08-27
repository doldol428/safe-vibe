"""안전모 감시 데모 — 프레임 소스 하나에서 AI 추론과 MJPEG 송출을 분리한다.

    FrameSource (Picamera2 / ffmpeg)
            |
        BGR Frame
        /        \\
    AI 추론      JPEG 인코딩
       |              |
    ROI 대조        MJPEG HTTP
       |              |
    이벤트          Browser

AI는 raw 프레임을 그대로 쓰고, 웹 송출용 JPEG는 따로 만든다.
MJPEG을 다시 디코드해서 추론에 쓰는 낭비가 없다.
"""
import json
import os
import re
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import detector as det
import frames
import roistore

HERE = Path(__file__).resolve().parent
INDEX_FILE = HERE / "index.html"
VIDEO = os.environ.get("VIDEO")     # 비우면 video/ 의 단일 mp4를 찾는다
PORT = int(os.environ.get("PORT", "8080"))
AI_FPS = float(os.environ.get("AI_FPS", "4"))
EVENT_CLASSES = [c.strip() for c in
                 os.environ.get("EVENT_CLASSES", "person").split(",") if c.strip()]

BOUNDARY = "frame"


# ---------------------------------------------------------------- 추론 워커

class DetectionWorker:
    """AI_FPS 로 최신 프레임을 샘플링해 추론하고, ROI와 대조해 이벤트를 낸다.

    송출 FPS와 추론 FPS를 분리해서, 화면은 부드럽게 두고 CPU는 추론에 필요한
    만큼만 쓴다. (Pi 5 CPU-only 기준 AI 3~5fps / 화면 10~15fps 권장)
    """

    def __init__(self, pipeline, model):
        self.pipeline = pipeline
        self.model = model
        self.lock = threading.Lock()
        self.detections = []
        self.roi_hits = {}
        self.infer_ms = 0.0
        self.ai_fps = 0.0
        self.events = deque(maxlen=50)
        self._active = set()        # 지금 무언가 잡혀 있는 ROI id
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def snapshot(self):
        with self.lock:
            return {
                "detections": list(self.detections),
                "roi_hits": dict(self.roi_hits),
                "infer_ms": round(self.infer_ms, 1),
                "ai_fps": self.ai_fps,
            }

    def event_list(self):
        with self.lock:
            return list(self.events)

    def _loop(self):
        interval = 1.0 / AI_FPS if AI_FPS > 0 else 0.25
        last_seq, ema, prev = -1, None, None
        while not self._stop.is_set():
            started = time.perf_counter()
            frame, seq = self.pipeline.latest()
            if frame is None or seq == last_seq:
                time.sleep(0.01)        # 아직 새 프레임이 없음
                continue
            last_seq = seq

            t0 = time.perf_counter()
            try:
                results = self.model.infer(frame)
            except Exception as e:      # 추론이 실패해도 송출은 계속되어야 한다
                print(f"[detect] 추론 실패: {e}", flush=True)
                time.sleep(0.5)
                continue
            infer_ms = (time.perf_counter() - t0) * 1000

            self._match_rois(results)

            now = time.perf_counter()
            if prev is not None:
                dt = now - prev
                ema = dt if ema is None else ema * 0.8 + dt * 0.2
                with self.lock:
                    self.ai_fps = round(1 / ema, 1) if ema > 0 else 0.0
            prev = now
            with self.lock:
                self.infer_ms = infer_ms

            sleep = interval - (time.perf_counter() - started)
            if sleep > 0:
                time.sleep(sleep)

    def _match_rois(self, results):
        """검출 박스의 발밑(하단 중앙)이 ROI 안에 있으면 그 ROI에 걸린 것으로 본다."""
        rois = [r for r in roistore.list_rois()
                if r["enabled"] and len(r["points"]) >= 3]
        hits = {r["id"]: 0 for r in rois}

        for d in results:
            x1, y1, x2, y2 = d["box"]
            foot = ((x1 + x2) / 2, y2)
            d["roi_ids"] = [r["id"] for r in rois
                            if roistore.point_in_polygon(*foot, r["points"])]
            for rid in d["roi_ids"]:
                hits[rid] += 1

        watched = {r["id"]: r["name"] for r in rois}
        now_active = {rid for rid, n in hits.items()
                      if n and any(d["name"] in EVENT_CLASSES
                                   for d in results if rid in d["roi_ids"])}
        with self.lock:
            self.detections = results
            self.roi_hits = hits
            # 비어 있다가 처음 잡힌 순간에만 이벤트를 남긴다 (매 프레임 아님)
            for rid in now_active - self._active:
                self.events.appendleft({
                    "ts": time.strftime("%H:%M:%S"),
                    "roi_id": rid,
                    "roi_name": watched.get(rid, "?"),
                    "count": hits[rid],
                })
                print(f"[event] {watched.get(rid)} 진입 ({hits[rid]}건)", flush=True)
            self._active = now_active


# ---------------------------------------------------------------- HTTP

ROI_ID_PATH = re.compile(r"^/api/rois/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    disable_nagle_algorithm = True   # 작은 JSON 응답이 지연되지 않도록 TCP_NODELAY

    pipeline = None      # main 에서 주입
    worker = None
    source_name = ""

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
            self._json({"rois": roistore.list_rois(), "max": roistore.MAX_ROI})
        elif self.path == "/api/detections":
            snap = self.worker.snapshot() if self.worker else {
                "detections": [], "roi_hits": {}, "infer_ms": 0, "ai_fps": 0}
            snap["fps"] = self.pipeline.fps
            self._json(snap)
        elif self.path == "/api/events":
            self._json({"events": self.worker.event_list() if self.worker else []})
        elif self.path == "/api/status":
            self._json(self.status())
        elif self.path in ("/", "/index.html"):
            self.serve_index()
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path != "/api/rois":
            return self.send_error(404)
        try:
            roi, err = roistore.create_roi(self._body())
        except (ValueError, json.JSONDecodeError):
            return self.send_error(400, "invalid json")
        self._json({"error": err} if err else roi, 409 if err else 201)

    def do_PUT(self):
        m = ROI_ID_PATH.match(self.path)
        if not m:
            return self.send_error(404)
        try:
            roi, err = roistore.update_roi(int(m.group(1)), self._body())
        except (ValueError, json.JSONDecodeError):
            return self.send_error(400, "invalid json")
        self._json({"error": err} if err else roi, 404 if err else 200)

    def do_DELETE(self):
        m = ROI_ID_PATH.match(self.path)
        if not m:
            return self.send_error(404)
        if not roistore.delete_roi(int(m.group(1))):
            return self.send_error(404, "no such roi")
        self._json({"deleted": int(m.group(1))})

    def status(self):
        model = self.worker.model if self.worker else None
        return {
            "source": self.source_name,
            "capture": [frames.CAPTURE_W, frames.CAPTURE_H],
            "stream": list(self.pipeline.stream_size or []),
            "stream_fps": frames.STREAM_FPS,
            "jpeg_quality": frames.JPEG_QUALITY,
            "ai_fps_target": AI_FPS,
            "model": model.path.name if model else None,
            "model_input": list(model.size) if model else None,
            "classes": len(model.names) if model else 0,
            "event_classes": EVENT_CLASSES,
        }

    def serve_index(self):
        try:
            body = INDEX_FILE.read_bytes()
        except FileNotFoundError:
            return self.send_error(500, "index.html not found")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def stream(self):
        """공유 파이프라인이 만든 JPEG를 multipart로 흘려보낸다."""
        self.send_response(200)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type",
                         f"multipart/x-mixed-replace; boundary={BOUNDARY}")
        self.end_headers()
        try:
            for jpeg in self.pipeline.hub.stream():
                self.wfile.write(
                    f"--{BOUNDARY}\r\nContent-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n\r\n".encode())
                self.wfile.write(jpeg)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass  # 클라이언트가 탭을 닫음

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- 시작

def main():
    try:
        source, source_name = frames.open_source(VIDEO)
    except (frames.NoVideoError, RuntimeError) as e:
        raise SystemExit(f"프레임 소스를 열 수 없습니다: {e}")
    pipeline = frames.Pipeline(source).start()

    try:
        model = det.Detector()
        worker = DetectionWorker(pipeline, model).start()
        print(f"model : {model.path.name} "
              f"({model.task}, {len(model.names)}클래스, 입력 {model.size[0]}x{model.size[1]})",
              flush=True)
    except det.NoModelError as e:
        model, worker = None, None
        print(f"model : 없음 — 검출 비활성 ({e})", flush=True)

    Handler.pipeline, Handler.worker, Handler.source_name = pipeline, worker, source_name

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

    print(f"source: {source_name} @ {frames.CAPTURE_W}x{frames.CAPTURE_H} "
          f"{frames.STREAM_FPS}fps (AI {AI_FPS}fps)", flush=True)
    print(f"roi   : {roistore.ROI_FILE} -> {len(roistore.list_rois())} ROI", flush=True)
    print(f"serving http://localhost:{PORT}", flush=True)
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
    finally:
        pipeline.stop()


if __name__ == "__main__":
    main()
