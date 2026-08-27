"""IoU 기반 경량 트래커.

vunexai-core `lib/iou.py` 초기 구현을 참고했다. Kalman 필터 없이 IoU 매칭만 쓰는
SORT 단순화판으로, 검출 수가 수십 개 이하인 환경에서는 이 정도로 충분하다.

원본과 다른 점:
  - 헝가리안 할당(lap/scipy)을 greedy 매칭으로 대체해 의존성을 없앴다.
    원본도 20개 이하에서는 greedy를 쓰므로 결과는 사실상 같다.
  - 클래스가 다른 검출-트랙은 매칭하지 않는다 (사람 트랙이 차로 바뀌는 것 방지).
  - 박스는 0~1 정규화 좌표. IoU는 스케일 무관이라 그대로 계산된다.

원본에서 가져온 수정 사항:
  - 빈 행렬일 때 (0,2) 형태를 돌려줘 IndexError를 막는다.
  - IoU 분모에 1e-6을 더해 0 나눗셈을 막는다.
"""
import numpy as np

from config import TRACK_IOU, TRACK_MAX_AGE, TRACK_MIN_HITS


def iou_batch(a, b):
    """(N,4) x (M,4) -> (N,M) IoU. 박스는 [x1, y1, x2, y2]."""
    a = np.expand_dims(np.asarray(a, np.float32), 1)
    b = np.expand_dims(np.asarray(b, np.float32), 0)

    xx1 = np.maximum(a[..., 0], b[..., 0])
    yy1 = np.maximum(a[..., 1], b[..., 1])
    xx2 = np.minimum(a[..., 2], b[..., 2])
    yy2 = np.minimum(a[..., 3], b[..., 3])
    wh = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)

    area_a = (a[..., 2] - a[..., 0]) * (a[..., 3] - a[..., 1])
    area_b = (b[..., 2] - b[..., 0]) * (b[..., 3] - b[..., 1])
    return wh / (area_a + area_b - wh + 1e-6)      # 0 나눗셈 방지


def greedy_match(iou, threshold):
    """IoU가 큰 쌍부터 차례로 짝짓는다. -> [(det_idx, trk_idx), ...]"""
    if iou.size == 0:
        return np.empty((0, 2), np.int32)          # 빈 행렬에서 IndexError 방지
    d_idx, t_idx = np.where(iou >= threshold)
    order = np.argsort(iou[d_idx, t_idx])[::-1]
    used_d, used_t, matches = set(), set(), []
    for k in order:
        d, t = int(d_idx[k]), int(t_idx[k])
        if d in used_d or t in used_t:
            continue
        used_d.add(d)
        used_t.add(t)
        matches.append((d, t))
    return np.array(matches, np.int32) if matches else np.empty((0, 2), np.int32)


class Track:
    def __init__(self, det, track_id, now):
        self.id = track_id
        self.box = det["box"]
        self.cls = det["cls"]
        self.name = det["name"]
        self.conf = det["conf"]
        self.hits = 1
        self.time_since_update = 0
        self.first_seen = now
        # ROI 체류 상태 — app.py가 채운다.
        self.roi_since = {}     # roi_id -> 진입 시각
        self.roi_left = {}      # roi_id -> 벗어난 시각 (경계 흔들림 디바운스용)
        self.roi_fired = set()  # 이미 이벤트를 낸 roi_id
        self.roi_in = set()     # 지금 실제로 안에 있는 roi_id (표시용)

    def update(self, det):
        self.box = det["box"]
        self.cls = det["cls"]
        self.name = det["name"]
        self.conf = det["conf"]
        self.hits += 1
        self.time_since_update = 0

    def to_dict(self, now):
        return {
            "id": self.id,
            "box": self.box,
            "cls": self.cls,
            "name": self.name,
            "conf": self.conf,
            "age": round(now - self.first_seen, 1),   # 처음 잡힌 뒤 지난 시간(초)
            "roi_ids": sorted(self.roi_in),
        }


class IOUTracker:
    def __init__(self, max_age=TRACK_MAX_AGE, min_hits=TRACK_MIN_HITS,
                 iou_threshold=TRACK_IOU):
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.tracks = []
        self._next_id = 1

    def update(self, dets, now):
        """검출 목록 -> 확정된 트랙 목록. dets는 detector.infer() 결과 형식."""
        for t in self.tracks:                       # SORT의 predict 단계에 해당
            t.time_since_update += 1

        matches = np.empty((0, 2), np.int32)
        if dets and self.tracks:
            iou = iou_batch([d["box"] for d in dets],
                            [t.box for t in self.tracks])
            # 클래스가 다르면 매칭 후보에서 제외
            d_cls = np.array([d["cls"] for d in dets])[:, None]
            t_cls = np.array([t.cls for t in self.tracks])[None, :]
            iou = np.where(d_cls == t_cls, iou, 0.0)
            matches = greedy_match(iou, self.iou_threshold)

        matched_d = set()
        for d, t in matches:
            self.tracks[t].update(dets[d])
            matched_d.add(d)

        for i, det in enumerate(dets):              # 짝을 못 찾은 검출 -> 새 트랙
            if i not in matched_d:
                self.tracks.append(Track(det, self._next_id, now))
                self._next_id += 1

        # 오래 안 보인 트랙은 버린다
        self.tracks = [t for t in self.tracks
                       if t.time_since_update <= self.max_age]

        # 이번 프레임에 갱신됐고 min_hits를 넘긴 트랙만 내보낸다
        return [t for t in self.tracks
                if t.time_since_update == 0 and t.hits >= self.min_hits]
