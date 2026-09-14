"""사람 자세(pose)로 몸/고개 방향을 판정한다.

박스만으로는 사람이 어느 쪽을 보는지 알 수 없다. 검출 모델(model/*.onnx)과 별도로
YOLOv8-pose 를 돌려 관절점(코·눈·귀·어깨)의 위치 관계로 방향을 정한다.

    프레임 ─ 검출 모델 ─ 트래커 ─ person 트랙 ──┐
       └──── pose 모델 ─ 사람 박스 + 관절점 17개 ─┴ IoU 매칭 -> 방향 판정 -> 트랙별 다수결

방향은 모두 '화면 기준'이다. right 는 화면 오른쪽을 향한다는 뜻이지 사람 기준의
오른쪽이 아니다. 관절점 이름의 l_/r_ 은 반대로 '사람 기준'이다 (COCO 규칙).
"""
import ast
from pathlib import Path

import cv2
import numpy as np

import detector as det
import tracker as trk
from config import (FACING_HEAD_RATIO, FACING_SIDE_RATIO, KP_CONF, POSE_CLASS,
                    POSE_CONF, POSE_MATCH_IOU)

KEYPOINTS = ("nose", "l_eye", "r_eye", "l_ear", "r_ear",
             "l_shoulder", "r_shoulder", "l_elbow", "r_elbow", "l_wrist", "r_wrist",
             "l_hip", "r_hip", "l_knee", "r_knee", "l_ankle", "r_ankle")
NOSE, L_EYE, R_EYE, L_EAR, R_EAR, L_SH, R_SH = range(7)


class PoseDetector(det.Detector):
    """YOLOv8-pose ONNX. 출력 (1, 5+3K, 8400) = 박스 4 + 사람 점수 1 + 관절점 K개(x, y, 신뢰도)."""

    def __init__(self, path):
        super().__init__(path=path, conf=POSE_CONF)
        if self.task != "pose":
            raise det.NoModelError(f"pose 모델이 아닙니다 (task={self.task}): {self.path.name}")
        meta = self.sess.get_modelmeta().custom_metadata_map
        try:
            k, dims = ast.literal_eval(meta["kpt_shape"])
        except (KeyError, ValueError, SyntaxError, TypeError):
            k, dims = len(KEYPOINTS), 3
        if (k, dims) != (len(KEYPOINTS), 3):
            # 방향 판정이 COCO 관절점 번호(코=0, 어깨=5/6)에 기대고 있다.
            raise det.NoModelError(
                f"COCO 17 관절점(x, y, 신뢰도) 모델만 지원합니다: kpt_shape=[{k}, {dims}]")
        self.num_kpts = k

    def infer(self, frame):
        """BGR 프레임 -> [{"conf", "box", "kp"}]. 좌표는 원본 프레임 기준 0~1."""
        out, geo = self._forward(frame)
        pred = self._as_rows(out)                  # (N, 56)
        if pred is None or not len(pred):
            return []
        conf = pred[:, 4]
        keep = conf >= self.conf
        if not keep.any():
            return []
        pred, conf = pred[keep], conf[keep]

        xywh = pred[:, :4].copy()
        xywh[:, 0] -= xywh[:, 2] / 2
        xywh[:, 1] -= xywh[:, 3] / 2
        idx = cv2.dnn.NMSBoxes(xywh.tolist(), conf.tolist(), self.conf, self.iou)
        if len(idx) == 0:
            return []

        people = []
        for i in np.array(idx).flatten():
            x, y, w, h = xywh[i]
            x1, y1 = self._unletterbox(x, y, geo)
            x2, y2 = self._unletterbox(x + w, y + h, geo)
            kp = pred[i, 5:].reshape(-1, 3)
            kx, ky = self._unletterbox(kp[:, 0], kp[:, 1], geo)
            people.append({
                "conf": round(float(conf[i]), 3),
                "box": [round(float(v), 5) for v in
                        (max(x1, 0), max(y1, 0), min(x2, 1), min(y2, 1))],
                "kp": [[round(float(a), 4), round(float(b), 4), round(float(c), 3)]
                       for a, b, c in zip(kx, ky, kp[:, 2])],
            })
        return people


def load(path):
    """pose 모델을 연다. -> (모델 또는 None, 못 연 이유)."""
    path = Path(path)
    if not path.is_file():
        return None, f"{path} 없음"
    try:
        return PoseDetector(path), None
    except Exception as e:      # 깨진 파일 등. pose 가 없어도 검출/경보는 돌아야 한다
        return None, str(e)


# ---------------------------------------------------------------- 방향 판정

def facing(kp, box):
    """관절점 -> 방향 판정과 그 근거.

    body  front | back | side | unknown   어깨 두 점의 좌우 순서와 간격으로 본다.
          정면이면 사람의 왼어깨가 화면 오른쪽에 온다(좌우 반전). 뒷모습이면 그대로다.
          간격이 좁으면 한쪽 어깨가 다른 쪽을 가리는 옆모습이다.
    head  left | right | center | unknown  코가 어깨 중심에서 어느 쪽으로 벗어났는지.

    둘을 합쳐 label 을 만든다: back + right -> "back-right", side + right -> "right".
    하나만 고르게 하면 뒤에서 비스듬히 보이는 모습에서 '오른쪽'이 사라진다.
    """
    kp = np.asarray(kp, np.float32)
    ok = kp[:, 2] >= KP_CONF
    x = kp[:, 0]
    w = max(box[2] - box[0], 1e-6)          # 거리마다 크기가 달라 박스 폭으로 정규화한다
    body, head, sh_dx, nose_off, mid = "unknown", "unknown", None, None, None

    if ok[L_SH] and ok[R_SH]:
        sh_dx = float((x[L_SH] - x[R_SH]) / w)
        body = ("front" if sh_dx >= FACING_SIDE_RATIO else
                "back" if sh_dx <= -FACING_SIDE_RATIO else "side")
        mid = (x[L_SH] + x[R_SH]) / 2
    elif ok[L_SH] or ok[R_SH]:
        body = "side"                        # 한쪽 어깨가 몸에 가려진 경우가 대부분이다
        mid = x[L_SH] if ok[L_SH] else x[R_SH]

    if ok[NOSE] and mid is not None:
        nose_off = float((x[NOSE] - mid) / w)
        head = ("right" if nose_off >= FACING_HEAD_RATIO else
                "left" if nose_off <= -FACING_HEAD_RATIO else "center")
    elif ok[NOSE] and (ok[L_EAR] != ok[R_EAR]):
        ear = x[L_EAR] if ok[L_EAR] else x[R_EAR]
        head = "right" if x[NOSE] > ear else "left"

    turned = head in ("left", "right")
    if body in ("front", "back"):
        label = f"{body}-{head}" if turned else body
    elif turned:
        label = head
    else:
        label = body                          # side 또는 unknown

    # 뒷모습인데 두 눈과 코가 다 보이면 정면을 좌우 뒤집어 읽었을 가능성이 크다.
    conflict = body == "back" and bool(ok[NOSE] and ok[L_EYE] and ok[R_EYE])
    return {"body": body, "head": head, "label": label, "conflict": conflict,
            "sh_dx": None if sh_dx is None else round(sh_dx, 3),
            "nose_off": None if nose_off is None else round(nose_off, 3),
            "kp_ok": int(ok.sum())}


def attach(tracks, people, now):
    """pose 결과를 사람 트랙에 붙인다. 이번에 짝이 없는 트랙은 pose 를 비운다.

    같은 프레임에서 나온 박스라 IoU 로 짝지으면 된다. 트래커와 같은 greedy 매칭을 쓴다.
    """
    targets = [t for t in tracks if t.name == POSE_CLASS]
    for t in targets:
        t.pose = None
    if not targets or not people:
        return
    iou = trk.iou_batch([p["box"] for p in people], [t.box for t in targets])
    for p_i, t_i in trk.greedy_match(iou, POSE_MATCH_IOU):
        p, t = people[p_i], targets[t_i]
        p.update(facing(p["kp"], p["box"]))
        t.pose = p
        t.facing_votes.append(p["label"])
        if p["sh_dx"] is not None:
            t.sh_dx_votes.append(p["sh_dx"])
        t.facing_log.append((round(now - t.first_seen, 1), p["label"]))
