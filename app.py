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
import signal
import socket
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config as cfg
import detector as det
import fall
import frames
import mqttpub
import pose
import roistore
import tracker as trk

BOUNDARY = "frame"      # multipart 경계 문자열 (설정이 아니라 프로토콜 값)


# ---------------------------------------------------------------- 추론 워커

class DetectionWorker:
    """AI_FPS 로 최신 프레임을 샘플링해 추론하고, ROI와 대조해 이벤트를 낸다.

    송출 FPS와 추론 FPS를 분리해서, 화면은 부드럽게 두고 CPU는 추론에 필요한
    만큼만 쓴다. (Pi 5 CPU-only 기준 AI 3~5fps / 화면 10~15fps 권장)
    """

    def __init__(self, pipeline, model, publisher, pose_model=None):
        self.pipeline = pipeline
        self.model = model
        self.publisher = publisher
        self.pose_model = pose_model    # 없으면 방향 분석을 건너뛴다
        self.tracker = trk.IOUTracker()
        self.lock = threading.Lock()
        self.detections = []
        self.people = []                # 분석 페이지용 사람 트랙 (관절점/방향 포함)
        self.roi_hits = {}
        self.infer_ms = 0.0
        self.pose_ms = None             # 이번 주기에 pose 를 안 돌렸으면 None
        self.falling_boxes = []         # 지금 떨어지는 중인 트랙 (화면 표시용)
        self.fall_last = {}             # 사람 트랙 id -> 마지막 낙하 경보 시각
        self.meter = frames.FpsMeter()
        self.events = deque(maxlen=cfg.EVENT_LOG_SIZE)
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, name="detect", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=3):
        """진행 중인 추론 한 번은 끝까지 돌리고 멈춘다."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)

    def snapshot(self):
        with self.lock:
            return {
                "detections": list(self.detections),
                "roi_hits": dict(self.roi_hits),
                "infer_ms": round(self.infer_ms, 1),
                "pose_ms": None if self.pose_ms is None else round(self.pose_ms, 1),
                "ai_fps": self.meter.value,
            }

    def analysis(self):
        with self.lock:
            return {
                "people": list(self.people),
                "falling_boxes": list(self.falling_boxes),
                "falls": [e for e in self.events if e.get("event") == "fall_warning"][:10],
                "infer_ms": round(self.infer_ms, 1),
                "pose_ms": None if self.pose_ms is None else round(self.pose_ms, 1),
                "ai_fps": self.meter.value,
            }

    def event_list(self):
        with self.lock:
            return list(self.events)

    def _loop(self):
        interval = 1.0 / cfg.AI_FPS if cfg.AI_FPS > 0 else 0.25
        last_seq = -1
        while not self._stop.is_set():
            started = time.perf_counter()
            frame, seq = self.pipeline.latest()
            if frame is None or seq == last_seq:
                self._stop.wait(0.01)   # 아직 새 프레임이 없음
                continue
            last_seq = seq

            t0 = time.perf_counter()
            try:
                results = self.model.infer(frame)
            except Exception as e:      # 추론이 실패해도 송출은 계속되어야 한다
                print(f"[detect] 추론 실패: {e}", flush=True)
                self._stop.wait(0.5)
                continue
            infer_ms = (time.perf_counter() - t0) * 1000

            # 체류 시간은 단조 시계로 잰다. time.time()은 NTP 동기화로 값이 점프해서
            # 체류 판정이 틀어질 수 있다 (Pi는 RTC가 없어 부팅 직후 크게 뛴다).
            now = time.monotonic()
            tracks = self.tracker.update(results, now)
            pose_ms = self._pose(frame, tracks, now)
            # 낙하와 체류가 같은 ROI 설정(알림 옵션)을 보도록 한 주기에 한 번만 읽는다.
            rois = [r for r in roistore.list_rois()
                    if r["enabled"] and len(r["points"]) >= 3]
            self._match_falls(tracks, now, rois)
            self._match_rois(tracks, now, rois)

            self.meter.tick()
            with self.lock:
                self.infer_ms = infer_ms
                self.pose_ms = pose_ms

            sleep = interval - (time.perf_counter() - started)
            if sleep > 0:
                self._stop.wait(sleep)  # stop() 하면 대기 중에도 바로 깬다

    def _pose(self, frame, tracks, now):
        """사람 트랙이 있을 때만 pose 를 돌려 트랙에 관절점/방향을 붙인다. -> 걸린 ms 또는 None.

        사람이 없는 프레임까지 돌리면 CPU 만 두 배로 쓴다.
        """
        if self.pose_model is None or not any(t.name == cfg.POSE_CLASS for t in tracks):
            return None
        t0 = time.perf_counter()
        try:
            pose.attach(tracks, self.pose_model.infer(frame), now)
        except Exception as e:          # pose 가 실패해도 검출/경보는 계속되어야 한다
            print(f"[pose] 추론 실패: {e}", flush=True)
            return None
        return (time.perf_counter() - t0) * 1000

    def _match_falls(self, tracks, now, rois):
        """떨어지는 박스를 찾아 가까운 사람의 왼쪽/오른쪽 진동 클라이언트로 경보를 낸다.

        판정은 fall.py 가 하고, 여기서는 같은 사람에게 연달아 울리지 않게 거르고 발행만 한다.
        pose 가 먼저 돌아야 사람 방향이 채워져 있으므로 _pose() 다음에 부른다.

        알림 여부는 사람이 서 있는 ROI 의 alert_fall 로 정한다. 겹친 ROI 중 하나라도 켜져
        있으면 울린다. 어느 ROI 에도 없으면 울린다 — ROI 를 안 그린 화면에서도 낙하는 경보여야 한다.
        """
        new_events = []
        for box, person, j in fall.detect(tracks):
            if now - self.fall_last.get(person.id, float("-inf")) < cfg.FALL_COOLDOWN_SEC:
                continue
            self.fall_last[person.id] = now
            inside = [r for r in rois if self._in_roi(person.box, r["points"])]
            alerting = [r for r in inside if r["alert_fall"]]
            roi = (alerting or inside or [None])[0]
            new_events.append({
                "event": "fall_warning",
                "ts": time.strftime("%H:%M:%S"),
                "ts_epoch": round(time.time(), 3),
                "track_id": box.id,
                "name": box.name,
                "person_id": person.id,
                **j,
                "roi_id": roi["id"] if roi else None,
                "roi_name": roi["name"] if roi else None,
                "alert": bool(alerting) or not inside,
            })

        with self.lock:
            for event in new_events:
                self.events.appendleft(event)

        for e in new_events:
            where = f", ROI {e['roi_name']}" if e["roi_name"] else ""
            action = (f"{fall.SIDE_KO[e['side']]} 진동" if e["alert"]
                      else "알림 끔 (ROI 설정)")
            print(f"[fall] #{e['track_id']} {e['name']} 낙하 -> 사람 #{e['person_id']} "
                  f"{action} (화면 {e['screen_side']}, "
                  f"방향 {e['facing']}, 근거 {e['basis']}, {e['speed']}/s{where})", flush=True)
            if not e["alert"]:
                continue
            # 양쪽이면 공통 토픽으로 보내 두 보드가 다 받게 한다.
            self.publisher.publish(e, subtopic=None if e["side"] == "both" else e["side"])

    @staticmethod
    def _in_roi(box, points):
        """config.ROI_MATCH 에 따라 트랙 박스가 ROI 에 걸렸는지 본다."""
        if cfg.ROI_MATCH == "foot":
            x1, _, x2, y2 = box
            return roistore.point_in_polygon((x1 + x2) / 2, y2, points)
        return roistore.box_overlap_ratio(box, points) > cfg.ROI_OVERLAP_MIN

    def _match_rois(self, tracks, now, rois):
        """트랙 박스가 ROI 에 걸리면(_in_roi) 그 ROI 안에 있는 것으로 본다.

        트랙 ID가 있으므로 "같은 사람이 계속 있는 것"과 "새로 들어온 것"을 구분할 수
        있다. 이벤트는 (트랙, ROI) 조합마다 체류 DWELL_SEC를 넘길 때 한 번만 낸다.
        이벤트는 항상 화면 목록에 남기고, 진동 알림(MQTT)은 ROI 의 alert_dwell 이 켜졌을 때만 낸다.
        """
        by_id = {r["id"]: r for r in rois}
        hits = {r["id"]: 0 for r in rois}
        fired = []

        for t in tracks:
            inside = {r["id"] for r in rois if self._in_roi(t.box, r["points"])}
            t.roi_in = inside

            for rid in inside:
                if rid not in t.roi_since:               # 새로 진입 -> 시각 기록
                    t.roi_since[rid] = now
                t.roi_left.pop(rid, None)                # 잠깐 벗어난 건 없던 일로

            for rid in set(t.roi_since) - inside:        # 벗어나 있는 중
                t.roi_left.setdefault(rid, now)
                if now - t.roi_left[rid] >= cfg.EXIT_SEC:    # 확실히 나감 -> 초기화
                    del t.roi_since[rid]
                    del t.roi_left[rid]
                    t.roi_fired.discard(rid)

            for rid in inside:
                hits[rid] += 1
                if (t.name in cfg.EVENT_CLASSES and rid not in t.roi_fired
                        and now - t.roi_since[rid] >= cfg.DWELL_SEC):
                    t.roi_fired.add(rid)
                    fired.append((t, rid))

        new_events = []
        with self.lock:
            self.detections = [t.to_dict(now) for t in tracks]
            self.people = [t.to_analysis(now) for t in tracks if t.name == cfg.POSE_CLASS]
            self.falling_boxes = [t.to_dict(now) for t in tracks if t.falling]
            self.roi_hits = hits
            for t, rid in fired:
                event = {
                    "event": "roi_dwell",
                    "ts": time.strftime("%H:%M:%S"),
                    "roi_id": rid,
                    "roi_name": by_id[rid]["name"],
                    "track_id": t.id,
                    "name": t.name,
                    "dwell": round(now - t.roi_since[rid], 1),
                    "alert": by_id[rid]["alert_dwell"],
                }
                self.events.appendleft(event)
                new_events.append(event)

        # 로그와 발행은 락을 놓고 한다. 브로커가 느릴 때 그 지연이 lock 을 붙들면
        # /api/detections 응답까지 같이 밀린다.
        for event in new_events:
            print(f"[event] {event['roi_name']} — #{event['track_id']} {event['name']} "
                  f"{cfg.DWELL_SEC}초 이상 체류"
                  f"{'' if event['alert'] else ' (알림 끔)'}", flush=True)
            if event["alert"]:
                self.publisher.publish(event)


# ---------------------------------------------------------------- HTTP

ROI_ID_PATH = re.compile(r"^/api/rois/(\d+)$")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    disable_nagle_algorithm = True   # 작은 JSON 응답이 지연되지 않도록 TCP_NODELAY

    pipeline = None      # main 에서 주입
    worker = None
    publisher = None
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
            self._json({"rois": roistore.list_rois(), "max": cfg.MAX_ROI})
        elif self.path == "/api/detections":
            snap = self.worker.snapshot() if self.worker else {
                "detections": [], "roi_hits": {}, "infer_ms": 0, "ai_fps": 0}
            snap["fps"] = self.pipeline.fps
            self._json(snap)
        elif self.path == "/api/events":
            self._json({"events": self.worker.event_list() if self.worker else []})
        elif self.path == "/api/status":
            self._json(self.status())
        elif self.path == "/api/analysis":
            self._json(self.analysis())
        elif self.path in ("/", "/index.html"):
            self.serve_html(cfg.INDEX_FILE)
        elif self.path in ("/analysis", "/analysis.html"):
            self.serve_html(cfg.ANALYSIS_FILE)
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/test-vibe":
            return self.test_vibe()
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
            "capture": [cfg.CAPTURE_W, cfg.CAPTURE_H],
            "stream": list(self.pipeline.stream_size or []),
            "stream_fps": cfg.STREAM_FPS,
            "jpeg_quality": cfg.JPEG_QUALITY,
            "ai_fps_target": cfg.AI_FPS,
            "model": model.path.name if model else None,
            "model_input": list(model.size) if model else None,
            "classes": len(model.names) if model else 0,
            "pose_model": (self.worker.pose_model.path.name
                           if self.worker and self.worker.pose_model else None),
            "event_classes": cfg.EVENT_CLASSES,
            "tracker": (f"IoU (max_age={cfg.TRACK_MAX_AGE}, min_hits={cfg.TRACK_MIN_HITS}, "
                        f"iou={cfg.TRACK_IOU})"),
            "dwell_sec": cfg.DWELL_SEC,
            "roi_match": ("foot" if cfg.ROI_MATCH == "foot"
                          else f"overlap > {cfg.ROI_OVERLAP_MIN:g}"),
            "mqtt": self.publisher.status() if self.publisher else {"enabled": False},
        }

    def test_vibe(self):
        """진동 클라이언트 배선·구독 확인용. 실제 경보와 같은 형식으로 한 건 발행한다.

        kind  fall  -> fall_warning 을 side 토픽으로 (left/right, both 는 공통 토픽). 낙하 패턴
              dwell -> roi_dwell 을 공통 토픽으로 (양쪽 보드). 체류 패턴
        보드는 (track_id, roi_id) 로 3초간 중복을 거르므로 누를 때마다 새 track_id 를 준다.
        화면 이벤트 목록과 분석 페이지 낙하 목록에는 남기지 않는다 — 감시 기록이 아니다.
        """
        try:
            body = self._body()
        except (ValueError, json.JSONDecodeError):
            return self._json({"error": "JSON 형식이 아닙니다"}, 400)
        kind, side = body.get("kind", "fall"), body.get("side", "both")
        if kind not in ("fall", "dwell") or side not in ("left", "right", "both"):
            return self._json({"error": "kind 는 fall|dwell, side 는 left|right|both"}, 400)

        mqtt = self.publisher.status() if self.publisher else {"enabled": False,
                                                               "reason": "MQTT 없음"}
        if not mqtt["enabled"]:
            return self._json({"error": f"MQTT 꺼짐 — {mqtt.get('reason')}"}, 503)
        if not mqtt["connected"]:
            return self._json({"error": f"브로커 {mqtt['broker']} 에 연결되지 않았습니다"}, 503)

        track_id = int(time.time() * 1000) % 1_000_000_000   # 보드의 long(32bit) 안에 든다
        common = {"test": True, "ts": time.strftime("%H:%M:%S"), "track_id": track_id,
                  "name": "테스트"}
        if kind == "fall":
            event = {"event": "fall_warning", **common, "person_id": 0, "side": side,
                     "screen_side": side, "basis": "test"}
            subtopic = None if side == "both" else side
        else:
            event = {"event": "roi_dwell", **common, "roi_id": 0, "roi_name": "진동 테스트",
                     "dwell": 0.0}
            subtopic = None
        topic = f"{self.publisher.topic}/{subtopic}" if subtopic else self.publisher.topic

        ok = self.publisher.publish(event, subtopic=subtopic)
        print(f"[test] 진동 테스트 {kind} {side} -> {topic} ({'발행' if ok else '실패'})",
              flush=True)
        if not ok:
            return self._json({"error": f"발행 실패 — {self.publisher.last_error}",
                               "topic": topic}, 502)
        self._json({"published": True, "topic": topic, "event": event})

    def analysis(self):
        """분석 페이지용. 사람 트랙마다 관절점, 방향 판정 근거, 최근 방향 이력을 준다."""
        pm = self.worker.pose_model if self.worker else None
        data = self.worker.analysis() if self.worker else {
            "people": [], "infer_ms": 0, "pose_ms": None, "ai_fps": 0}
        data.update({
            "fps": self.pipeline.fps,
            "source": self.source_name,
            "pose_model": pm.path.name if pm else None,
            "pose_model_path": str(cfg.POSE_MODEL),
            "keypoints": pose.KEYPOINTS,
            "kp_conf": cfg.KP_CONF,
            "side_ratio": cfg.FACING_SIDE_RATIO,
            "head_ratio": cfg.FACING_HEAD_RATIO,
            "window": cfg.FACING_WINDOW,
            "fall": {"class": cfg.FALL_CLASS, "min_drop": cfg.FALL_MIN_DROP,
                     "min_speed": cfg.FALL_MIN_SPEED, "near_ratio": cfg.FALL_NEAR_RATIO,
                     "center_ratio": cfg.FALL_CENTER_RATIO},
        })
        return data

    def serve_html(self, path):
        try:
            body = path.read_bytes()
        except FileNotFoundError:
            return self.send_error(500, f"{path.name} not found")
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
        except OSError:
            # 클라이언트가 탭을 닫음. Windows는 ConnectionAbortedError(10053),
            # Linux는 BrokenPipeError를 낸다. 전부 OSError 하위라 한 번에 잡는다.
            pass

    def log_message(self, fmt, *args):
        pass


# ---------------------------------------------------------------- 시작

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


class Shutdown:
    """Ctrl+C(SIGINT) / SIGTERM / Ctrl+Break(SIGBREAK, Windows)를 받아 정리 후 종료한다.

    서버가 떠 있으면 KeyboardInterrupt 대신 httpd.shutdown()으로 serve_forever를
    정상 반환시킨다. shutdown()은 serve_forever가 끝나길 기다리는데, 핸들러는 바로
    그 serve_forever를 돌리는 메인 스레드에서 실행되므로 직접 부르면 교착된다.
    그래서 별도 스레드에서 부른다.
    """

    def __init__(self):
        self.httpd = None
        self.requested = False

    def install(self):
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            if hasattr(signal, name):           # SIGBREAK는 Windows에만 있다
                signal.signal(getattr(signal, name), self._handle)
        return self

    def _handle(self, signum, frame):
        name = signal.Signals(signum).name
        if self.requested:
            # 정리가 어딘가에서 멈췄을 때를 위한 탈출구
            print(f"[main] {name} 재수신 — 강제 종료", flush=True)
            os._exit(1)
        self.requested = True
        print(f"\n[main] {name} 수신 — 종료 중 (한 번 더 누르면 강제 종료)", flush=True)
        if self.httpd is None:
            raise KeyboardInterrupt             # 서버가 뜨기 전(모델 로딩 등)이면 바로 빠져나간다
        threading.Thread(target=self.httpd.shutdown, daemon=True).start()


def main():
    shutdown = Shutdown().install()
    try:
        source, source_name = frames.open_source(cfg.VIDEO)
    except (frames.NoVideoError, RuntimeError) as e:
        raise SystemExit(f"프레임 소스를 열 수 없습니다: {e}")

    # 여기서부터 만든 것들은 어떤 경로로 빠져나가든 finally에서 닫는다.
    # ffmpeg는 Ctrl+C를 같이 받지 않도록 떼어놨으므로(frames.FfmpegSource),
    # 여기서 안 닫으면 고아 프로세스로 남는다.
    pipeline = frames.Pipeline(source).start()
    publisher = worker = httpd = None
    try:
        publisher = mqttpub.Publisher(cfg.MQTT_HOST, cfg.MQTT_PORT, cfg.MQTT_TOPIC,
                                      qos=cfg.MQTT_QOS,
                                      client_id=cfg.MQTT_CLIENT_ID).start()

        try:
            model = det.Detector()
            print(f"model : {model.path.name} "
                  f"({model.task}, {len(model.names)}클래스, 입력 {model.size[0]}x{model.size[1]})",
                  flush=True)
            pose_model, why = pose.load(cfg.POSE_MODEL)
            print(f"pose  : {pose_model.path.name} (관절점 {pose_model.num_kpts}개, "
                  f"'{cfg.POSE_CLASS}' 트랙에 방향 판정)" if pose_model
                  else f"pose  : 없음 — 방향 분석 비활성 ({why})", flush=True)
            worker = DetectionWorker(pipeline, model, publisher, pose_model).start()
        except det.NoModelError as e:
            print(f"model : 없음 — 검출 비활성 ({e})", flush=True)

        Handler.pipeline, Handler.worker, Handler.source_name = pipeline, worker, source_name
        Handler.publisher = publisher

        print(f"source: {source_name} @ {cfg.CAPTURE_W}x{cfg.CAPTURE_H} "
              f"{cfg.STREAM_FPS}fps (AI {cfg.AI_FPS}fps)", flush=True)
        match = "발밑 점" if cfg.ROI_MATCH == "foot" else f"겹침 비율 > {cfg.ROI_OVERLAP_MIN:g}"
        print(f"roi   : {roistore.ROI_FILE} -> {len(roistore.list_rois())} ROI (판정: {match})",
              flush=True)
        print(f"serving http://localhost:{cfg.PORT}  (방향 분석: /analysis)", flush=True)
        try:
            # OSError를 잡아서 폴백하면 "포트 사용 중" 에러까지 삼켜버리므로,
            # IPv6 지원 여부는 미리 물어보고 고른다.
            httpd = (DualStackServer(("::", cfg.PORT), Handler)
                     if socket.has_dualstack_ipv6()
                     else Server(("0.0.0.0", cfg.PORT), Handler))
        except OSError as e:
            raise SystemExit(f"포트 {cfg.PORT} 바인딩 실패 (이미 실행 중?): {e}")
        shutdown.httpd = httpd
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 받는 쪽부터 닫는다: HTTP -> 추론 -> 캡처(ffmpeg) -> MQTT
        if httpd is not None:
            httpd.server_close()
        if worker is not None:
            worker.stop()
        pipeline.stop()
        if publisher is not None:
            publisher.stop()
    print("[main] 정리 완료", flush=True)


if __name__ == "__main__":
    main()
