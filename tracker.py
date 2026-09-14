"""IoU 기반 경량 트래커.

Kalman 필터 없이 IoU 매칭만 쓰는 SORT 단순화판이다. 예측 모델이 없어 비용이
거의 들지 않고, 한 화면의 검출 수가 수십 개 이하면 이 정도로 충분하다.

설계 메모:
  - 할당은 헝가리안 대신 greedy. 검출 수가 적으면 결과가 사실상 같으면서
    lap/scipy 의존성을 지울 수 있다.
  - 클래스가 다른 검출-트랙은 매칭하지 않는다 (사람 트랙이 차로 바뀌는 것 방지).
  - 박스는 0~1 정규화 좌표. IoU는 스케일 무관이라 그대로 계산된다.
  - 빈 행렬은 (0,2) 형태로 돌려주고 IoU 분모에 1e-6을 더한다.
    각각 IndexError와 0 나눗셈을 막기 위한 것으로, 둘 다 실제로 밟기 쉽다.
"""
import collections
import math

import numpy as np

from config import (FACING_HISTORY, FACING_WINDOW, TRACK_IOU, TRACK_MAX_AGE,
                    TRACK_MIN_HITS, TRACK_TRAIL)


def box_center(box):
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


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
        # 중심점 이력 (시각, cx, cy). 낙하처럼 '움직임'으로 판정하는 이벤트가 쓴다.
        self.trail = collections.deque([(now, *box_center(self.box))], maxlen=TRACK_TRAIL)
        self.falling = False       # 지금 떨어지는 중인지 — fall.py 판정을 app.py 가 채운다
        self.fall_fired = False    # 이 트랙으로 낙하 경보를 이미 냈는지
        self.near_head = False     # 지금 누군가의 머리 주변에서 움직이는 중인지 — head.py
        self.head_fired = set()    # 이 박스로 머리 근접 경보를 이미 낸 사람 트랙 id
        # ROI 체류 상태 — app.py가 채운다.
        self.roi_since = {}     # roi_id -> 진입 시각
        self.roi_left = {}      # roi_id -> 벗어난 시각 (경계 흔들림 디바운스용)
        self.roi_fired = set()  # 이미 이벤트를 낸 roi_id
        self.roi_in = set()     # 지금 실제로 안에 있는 roi_id (표시용)
        # 자세/방향 상태 — pose.py 가 채운다.
        self.pose = None                                          # 이번 추론의 관절점과 판정
        self.facing_votes = collections.deque(maxlen=FACING_WINDOW)
        self.facing_log = collections.deque(maxlen=FACING_HISTORY)  # (처음 잡힌 뒤 초, label)
        self.sh_dx_votes = collections.deque(maxlen=FACING_WINDOW)  # 어깨 간격 — 몸이 앞/뒤 어느 쪽인지

    def update(self, det, now):
        self.box = det["box"]
        self.cls = det["cls"]
        self.name = det["name"]
        self.conf = det["conf"]
        self.hits += 1
        self.time_since_update = 0
        self.trail.append((now, *box_center(self.box)))

    def motion(self, window):
        """-> (drop, speed). 화면 높이 기준이고 아래가 + 다.

        drop  최근 window 초 동안 내려간 양. 검출 흔들림(±0.01)은 쌓이지 않아 여기서 걸러진다.
        speed 직전 두 점 사이의 하강 속도. window 전체 평균으로 재면 떨어지기 직전까지
              가만히 있던 구간이 섞여 실제보다 느리게 나온다.
        프레임 수가 아니라 시각으로 재므로 AI_FPS 가 바뀌어도 기준값을 다시 잡지 않아도 된다.
        """
        t1, _, y1 = self.trail[-1]
        old = next(e for e in self.trail if t1 - e[0] <= window)
        drop = y1 - old[2]
        if len(self.trail) < 2:
            return drop, 0.0
        t0, _, y0 = self.trail[-2]
        return drop, ((y1 - y0) / (t1 - t0) if t1 > t0 else 0.0)

    def travel(self, window, aspect=1.0):
        """-> (move, speed). motion() 의 방향 무관판. 화면 높이 단위.

        move  최근 window 초 동안 중심이 움직인 직선거리
        speed 직전 두 점 사이 속도
        좌표는 0~1 정규화라 가로 0.01 과 세로 0.01 의 실제 픽셀이 다르다(16:9 면 1.78배).
        가로에 aspect(가로/세로)를 곱해 세로와 같은 척도로 맞춘다.
        """
        t1, x1, y1 = self.trail[-1]
        old = next(e for e in self.trail if t1 - e[0] <= window)
        move = math.hypot((x1 - old[1]) * aspect, y1 - old[2])
        if len(self.trail) < 2:
            return move, 0.0
        t0, x0, y0 = self.trail[-2]
        if t1 <= t0:
            return move, 0.0
        return move, math.hypot((x1 - x0) * aspect, y1 - y0) / (t1 - t0)

    def to_dict(self, now):
        return {
            "id": self.id,
            "box": self.box,
            "cls": self.cls,
            "name": self.name,
            "conf": self.conf,
            "age": round(now - self.first_seen, 1),   # 처음 잡힌 뒤 지난 시간(초)
            "roi_ids": sorted(self.roi_in),
            "facing": self.facing(),
            "falling": self.falling,
            "near_head": self.near_head,
        }

    def facing(self):
        """최근 FACING_WINDOW 번 판정의 다수결. 판정 불가(unknown)는 표에서 뺀다."""
        votes = [v for v in self.facing_votes if v != "unknown"]
        if not votes:
            return None
        return collections.Counter(votes).most_common(1)[0][0]

    def body_axis(self):
        """최근 어깨 간격(sh_dx) 평균. + 면 카메라 쪽(정면), - 면 등, 0 근처면 옆모습. 없으면 None.

        화면 표시용 label 은 비스듬한 뒷모습에서 back-right 와 right 사이를 오간다.
        좌/우 판정은 그 경계에 휘둘리면 안 되므로 값 자체의 평균을 따로 쓴다.
        """
        if not self.sh_dx_votes:
            return None
        return sum(self.sh_dx_votes) / len(self.sh_dx_votes)

    def to_analysis(self, now):
        """분석 페이지용. to_dict 에 이번 관절점/판정 근거와 방향 이력을 더한다."""
        d = self.to_dict(now)
        d["pose"] = self.pose
        d["facing_log"] = [list(e) for e in self.facing_log]
        return d


class IOUTracker:
    def __init__(self, max_age=TRACK_MAX_AGE, min_hits=TRACK_MIN_HITS,
                 iou_threshold=TRACK_IOU):
        self.max_age = max_age
        self.min_hits = min_hits
        self.iou_threshold = iou_threshold
        self.tracks = []
        self._next_id = 1

    def update(self, dets, now):
        """검출 목록 -> 확정된 트랙 목록. dets는 detector.infer() 결과 형식.

        now는 단조 시계(time.monotonic) 기준 초. 벽시계를 넣으면 시각 점프에
        체류 시간 계산이 흔들린다.
        """
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
            self.tracks[t].update(dets[d], now)
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
