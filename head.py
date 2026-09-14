"""움직이는 박스가 사람 머리 가까이 오면 경보한다 — 좌/우 방향 판단 없이 양쪽으로.

fall.py 는 '아래로 떨어지는' 박스만 보고 몸 기준 좌/우까지 가린다. 옆에서 던지거나
비스듬히 날아오는 박스는 아래로 충분히 내려오지 않아 거기에 안 걸린다. 여기서는 더
단순하게, 머리 주변 영역에 '움직이는' 박스가 들어오면 울린다.

    머리 영역 (사람 박스 윗변 기준, 사람 폭 w / 키 h 의 배수)
        가로  몸 중심 ± HEAD_ZONE_W × w
        세로  윗변 - HEAD_ZONE_UP × h  ~  윗변 + HEAD_ZONE_DOWN × h

움직임을 조건으로 두는 이유: 선반에 쌓인 박스도 화면에서는 머리 옆에 걸린다.
가만히 있는 박스까지 울리면 선반 앞을 지날 때마다 진동한다.
"""
import fall
from config import (CAPTURE_H, CAPTURE_W, HEAD_CLASS, HEAD_MIN_MOVE, HEAD_MIN_SPEED,
                    HEAD_WINDOW_SEC, HEAD_ZONE_DOWN, HEAD_ZONE_UP, HEAD_ZONE_W,
                    POSE_CLASS)

ASPECT = CAPTURE_W / CAPTURE_H


def zone(person):
    """사람 트랙 -> 머리 영역 [x1, y1, x2, y2] (0~1)."""
    x1, y1, x2, y2 = person.box
    w, h = x2 - x1, y2 - y1
    cx = fall.body_center_x(person)       # 어깨가 보이면 어깨 중점 — 팔을 뻗어도 안 흔들린다
    return [cx - HEAD_ZONE_W * w, y1 - HEAD_ZONE_UP * h,
            cx + HEAD_ZONE_W * w, y1 + HEAD_ZONE_DOWN * h]


def moving(track):
    """HEAD_CLASS 트랙이 움직이는 중이면 (move, speed), 아니면 None."""
    if track.name != HEAD_CLASS or len(track.trail) < 2:
        return None
    move, speed = track.travel(HEAD_WINDOW_SEC, ASPECT)
    if move >= HEAD_MIN_MOVE and speed >= HEAD_MIN_SPEED:
        return move, speed
    return None


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def detect(tracks):
    """이번 추론에서 새로 생긴 머리 근접 -> [(박스 트랙, 사람 트랙, 근거)].

    (박스, 사람) 쌍마다 한 번만 낸다. 머리 영역을 지나는 동안 매 추론마다 조건을 만족하기 때문이다.
    """
    people = [t for t in tracks if t.name == POSE_CLASS]
    found = []
    for box in tracks:
        m = moving(box)
        box.near_head = False
        if not m:
            continue
        for person in people:
            if not _overlaps(box.box, zone(person)):
                continue
            box.near_head = True
            if person.id in box.head_fired:
                continue
            box.head_fired.add(person.id)
            found.append((box, person, {"move": round(m[0], 3), "speed": round(m[1], 3)}))
    return found
