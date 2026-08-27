"""프레임 소스와 공유 파이프라인.

    FrameSource (BGR 프레임)
        ├─ AI 추론용 raw 프레임      (app.py 의 DetectionWorker 가 가져감)
        └─ JPEG 인코딩 -> MJPEG 송출 (FrameHub 가 접속자 전원에게 fan-out)

AI용 raw 프레임과 모니터링용 JPEG를 분리해서, MJPEG을 다시 디코드하는 낭비가 없다.
카메라가 바뀌어도(FfmpegSource <-> PicameraSource) 위쪽 코드는 손대지 않는다.
"""
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import cv2
import numpy as np

CAPTURE_W = int(os.environ.get("CAPTURE_W", "1280"))
CAPTURE_H = int(os.environ.get("CAPTURE_H", "720"))
STREAM_FPS = float(os.environ.get("STREAM_FPS", "15"))
JPEG_QUALITY = int(os.environ.get("JPEG_QUALITY", "75"))
# 송출용 가로 해상도. AI는 캡처 원본을 쓰고 브라우저에만 축소본을 보낸다.
# 0이면 캡처 해상도 그대로. 720p q75는 약 20Mbps, 960 폭은 약 12Mbps.
STREAM_W = int(os.environ.get("STREAM_W", "960"))


class FrameSource:
    """BGR(HxWx3, uint8) 프레임을 내보낸다. BGR인 이유는 cv2 기본 순서라서."""

    def read(self):
        raise NotImplementedError

    def close(self):
        pass


class FfmpegSource(FrameSource):
    """개발 PC용. 영상 파일을 무한 반복하며 실시간 속도로 재생한다."""

    def __init__(self, video, width=CAPTURE_W, height=CAPTURE_H, fps=STREAM_FPS):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg를 PATH에서 찾을 수 없습니다")
        self.video = Path(video)
        if not self.video.exists():
            raise RuntimeError(f"영상 파일 없음: {self.video}")
        self.width, self.height, self.fps = width, height, fps
        self.frame_bytes = width * height * 3
        self.proc = None
        self._spawn()

    def _spawn(self):
        self.proc = subprocess.Popen([
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-stream_loop", "-1",       # 무한 반복
            "-re",                      # 실시간 속도
            "-i", str(self.video),
            "-an",
            "-r", str(self.fps),
            "-vf", f"scale={self.width}:{self.height}",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1",
        ], stdout=subprocess.PIPE, bufsize=0)

    def read(self):
        # 파이프의 read()는 요청보다 적게 돌려줄 수 있으므로 한 프레임을 채울 때까지 읽는다.
        buf = bytearray()
        while len(buf) < self.frame_bytes:
            chunk = self.proc.stdout.read(self.frame_bytes - len(buf))
            if not chunk:               # EOF — ffmpeg가 죽었으면 다시 띄운다
                self.close()
                self._spawn()
                return None
            buf += chunk
        return np.frombuffer(bytes(buf), np.uint8).reshape(
            self.height, self.width, 3)

    def close(self):
        if not self.proc:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


class PicameraSource(FrameSource):
    """라즈베리파이 Camera Module 3 용. picamera2 가 설치된 환경에서만 동작."""

    def __init__(self, width=CAPTURE_W, height=CAPTURE_H, fps=STREAM_FPS):
        from picamera2 import Picamera2      # Pi 밖에서는 import 자체가 실패한다

        self.cam = Picamera2()
        # picamera2의 "RGB888"은 numpy에서 BGR 순서로 나온다 (알려진 표기 혼동).
        # 즉 이 설정이 곧 cv2 기본 순서라 별도 변환이 필요 없다.
        cfg = self.cam.create_video_configuration(
            main={"size": (width, height), "format": "RGB888"},
            controls={"FrameDurationLimits": (int(1e6 / fps), int(1e6 / fps))},
        )
        self.cam.configure(cfg)
        self.cam.start()

    def read(self):
        return self.cam.capture_array()

    def close(self):
        self.cam.stop()
        self.cam.close()


def open_source(video=None, kind=None):
    """SOURCE 환경변수로 고르되, 기본은 picamera2가 있으면 카메라, 없으면 영상 파일."""
    kind = (kind or os.environ.get("SOURCE") or "auto").lower()
    if kind == "auto":
        try:
            import picamera2  # noqa: F401
            kind = "picamera"
        except ImportError:
            kind = "video"
    if kind == "picamera":
        return PicameraSource(), "picamera2"
    return FfmpegSource(video), f"ffmpeg:{Path(video).name}"


# ---------------------------------------------------------------- 송출

class FrameHub:
    """인코딩된 JPEG 한 장을 접속자 전원에게 나눠준다 (접속당 인코딩 안 함)."""

    def __init__(self):
        self._cond = threading.Condition()
        self._jpeg = None
        self._seq = 0

    def publish(self, jpeg):
        with self._cond:
            self._jpeg = jpeg
            self._seq += 1
            self._cond.notify_all()

    def stream(self):
        """접속자마다 하나씩. 최신 프레임만 보내므로 느린 클라이언트는 프레임을 건너뛴다."""
        last = -1
        while True:
            with self._cond:
                if not self._cond.wait_for(lambda: self._seq != last, timeout=10):
                    return                      # 10초간 새 프레임 없음 -> 종료
                last, jpeg = self._seq, self._jpeg
            yield jpeg


class Pipeline:
    """캡처 스레드 하나가 raw 프레임 보관 + JPEG 인코딩을 담당한다."""

    def __init__(self, source, quality=JPEG_QUALITY, stream_w=STREAM_W):
        self.source = source
        self.quality = quality
        self.stream_w = stream_w
        self.stream_size = None      # 첫 프레임에서 결정
        self.hub = FrameHub()
        self._lock = threading.Lock()
        self._frame = None
        self._frame_seq = 0
        self.fps = 0.0
        self._stop = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def stop(self):
        self._stop.set()
        self.source.close()

    def latest(self):
        """AI 추론용 raw BGR 프레임. (frame, seq) — seq로 같은 프레임 재추론을 피한다."""
        with self._lock:
            return self._frame, self._frame_seq

    def _for_stream(self, frame):
        """송출용 축소. 브라우저는 원본 해상도가 필요 없고 대역폭만 먹는다."""
        h, w = frame.shape[:2]
        if not self.stream_w or self.stream_w >= w:
            self.stream_size = (w, h)
            return frame
        if self.stream_size is None or self.stream_size[0] != self.stream_w:
            self.stream_size = (self.stream_w, round(h * self.stream_w / w) // 2 * 2)
        return cv2.resize(frame, self.stream_size, interpolation=cv2.INTER_AREA)

    def _loop(self):
        ema, prev = None, None
        while not self._stop.is_set():
            frame = self.source.read()
            if frame is None:
                continue
            with self._lock:
                self._frame = frame          # AI는 축소 전 원본을 쓴다
                self._frame_seq += 1
            ok, buf = cv2.imencode(
                ".jpg", self._for_stream(frame),
                [cv2.IMWRITE_JPEG_QUALITY, self.quality])
            if ok:
                self.hub.publish(buf.tobytes())

            now = time.perf_counter()
            if prev is not None:
                dt = now - prev
                ema = dt if ema is None else ema * 0.9 + dt * 0.1
                self.fps = round(1 / ema, 1) if ema > 0 else 0.0
            prev = now
