"""model/ 디렉터리의 단일 ONNX 검출 모델을 onnxruntime으로 서빙한다.

Triton 없이 python onnxruntime 만 쓴다. 모델은 디렉터리에 하나만 두는 것이 규칙이고,
클래스 이름은 하드코딩하지 않고 ONNX 메타데이터에서 읽으므로 모델을 교체해도
코드를 고칠 필요가 없다 (COCO YOLOv8m -> 안전모 모델 등).
"""
import ast
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

from config import CONF_THRESHOLD, MODEL_DIR, NMS_IOU, ORT_THREADS


class NoModelError(RuntimeError):
    pass


class Detector:
    def __init__(self, model_dir=MODEL_DIR,
                 conf=CONF_THRESHOLD, iou=NMS_IOU):
        self.path = self._find_model(Path(model_dir))
        self.conf, self.iou = conf, iou

        opts = ort.SessionOptions()
        if ORT_THREADS:
            opts.intra_op_num_threads = ORT_THREADS
        # 기본값이면 추론 사이에 스레드풀이 CPU를 태우며 대기한다(spin-wait).
        # 캡처/JPEG 인코딩 스레드와 코어를 두고 싸우면서 추론이 2배 이상 느려지므로 끈다.
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
        self.sess = ort.InferenceSession(
            str(self.path), opts, providers=["CPUExecutionProvider"])

        inp = self.sess.get_inputs()[0]
        self.input_name = inp.name
        # [1, 3, H, W] — 동적 축이면 숫자가 아니라 문자열이 들어있다.
        h, w = inp.shape[2], inp.shape[3]
        self.size = (int(w) if isinstance(w, int) else 640,
                     int(h) if isinstance(h, int) else 640)

        meta = self.sess.get_modelmeta().custom_metadata_map
        self.names = self._parse_names(meta.get("names"))
        self.task = meta.get("task", "detect")
        self.description = meta.get("description", self.path.name)

    @staticmethod
    def _find_model(model_dir):
        if not model_dir.is_dir():
            raise NoModelError(f"모델 디렉터리가 없습니다: {model_dir}")
        found = sorted(model_dir.glob("*.onnx"))
        if not found:
            raise NoModelError(f"{model_dir} 에 .onnx 파일이 없습니다")
        if len(found) > 1:
            raise NoModelError(
                f"{model_dir} 에 .onnx가 여러 개입니다(단일 모델만 지원): "
                + ", ".join(f.name for f in found))
        return found[0]

    @staticmethod
    def _parse_names(raw):
        """ultralytics는 {0: 'person', ...} 를 문자열로 넣어둔다."""
        try:
            names = ast.literal_eval(raw)
            return {int(k): str(v) for k, v in names.items()}
        except (ValueError, SyntaxError, AttributeError, TypeError):
            return {}

    def name_of(self, cls_id):
        return self.names.get(cls_id, f"class_{cls_id}")

    # ------------------------------------------------------------ 전처리

    def _letterbox(self, frame):
        """비율을 유지한 채 모델 입력 크기로 맞추고 남는 곳은 회색으로 채운다."""
        h0, w0 = frame.shape[:2]
        tw, th = self.size
        scale = min(tw / w0, th / h0)
        nw, nh = round(w0 * scale), round(h0 * scale)
        resized = cv2.resize(frame, (nw, nh), interpolation=cv2.INTER_LINEAR)
        canvas = np.full((th, tw, 3), 114, np.uint8)
        dx, dy = (tw - nw) // 2, (th - nh) // 2
        canvas[dy:dy + nh, dx:dx + nw] = resized
        return canvas, scale, dx, dy

    # ------------------------------------------------------------ 추론

    def infer(self, frame):
        """BGR 프레임 -> 검출 목록.

        박스는 원본 프레임 기준 0~1 정규화 좌표(x1,y1,x2,y2)로 돌려준다.
        ROI 좌표계와 같은 단위라 그대로 대조할 수 있다.
        """
        h0, w0 = frame.shape[:2]
        canvas, scale, dx, dy = self._letterbox(frame)
        blob = cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)
        blob = (blob.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]

        out = self.sess.run(None, {self.input_name: blob})[0]
        pred = self._as_rows(out)                  # (N, 4+num_classes)
        if pred is None or not len(pred):
            return []

        boxes_xywh, scores = pred[:, :4], pred[:, 4:]
        cls = scores.argmax(1)
        conf = scores.max(1)
        keep = conf >= self.conf
        if not keep.any():
            return []
        boxes_xywh, cls, conf = boxes_xywh[keep], cls[keep], conf[keep]

        # cx,cy,w,h -> x,y,w,h (cv2 NMS 입력 형식)
        xywh = boxes_xywh.copy()
        xywh[:, 0] -= xywh[:, 2] / 2
        xywh[:, 1] -= xywh[:, 3] / 2
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), conf.tolist(), self.conf, self.iou)
        if len(idx) == 0:
            return []
        idx = np.array(idx).flatten()

        results = []
        for i in idx:
            x, y, w, h = xywh[i]
            # letterbox 되돌리기 -> 원본 픽셀 -> 정규화
            x1 = (x - dx) / scale / w0
            y1 = (y - dy) / scale / h0
            x2 = (x + w - dx) / scale / w0
            y2 = (y + h - dy) / scale / h0
            results.append({
                "cls": int(cls[i]),
                "name": self.name_of(int(cls[i])),
                "conf": round(float(conf[i]), 3),
                "box": [round(float(v), 5) for v in
                        (max(x1, 0), max(y1, 0), min(x2, 1), min(y2, 1))],
            })
        results.sort(key=lambda d: -d["conf"])
        return results

    @staticmethod
    def _as_rows(out):
        """YOLOv8 detect 출력을 (N, 4+classes) 로 정규화한다.

        ultralytics export는 (1, 84, 8400), 일부 변환기는 (1, 8400, 84)로 낸다.
        """
        if out.ndim != 3:
            return None
        arr = out[0]
        if arr.shape[0] < arr.shape[1]:     # (84, 8400)
            return arr.T
        return arr                          # (8400, 84)
