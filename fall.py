"""떨어지는 물체를 잡아 사람의 왼쪽/오른쪽 중 어느 쪽인지 판정한다.

    FALL_CLASS 트랙 중심 이력 ─ 아래로 빠르게 움직이나? ─ 가장 가까운 사람
        └ 사람 몸 중심 대비 화면 좌/우 ─ 사람 방향(어깨 간격)으로 몸 기준 좌/우로 바꾼다
                                              └ {"event": "fall_warning", "side": ...}

화면의 좌/우와 사람의 좌/우는 다르다. 어깨 간격 sh_dx(pose.facing) 평균으로 가른다.

    sh_dx <= -FALL_PROFILE_RATIO   등이 카메라 쪽      화면 좌/우 = 몸 좌/우        basis=back
    sh_dx >= +FALL_PROFILE_RATIO   얼굴이 카메라 쪽    거울처럼 반대               basis=front
    그 사이                        옆모습             화면 좌/우가 사람의 앞/뒤가 되고 몸의
                                                      좌/우는 깊이 방향이라 알 수 없다 -> 양쪽
    어깨를 못 봄 (pose 없음)                           화면 기준 그대로             basis=screen
"""
from config import (FALL_CENTER_RATIO, FALL_CLASS, FALL_MIN_DROP, FALL_MIN_SPEED,
                    FALL_NEAR_RATIO, FALL_PROFILE_RATIO, FALL_WINDOW_SEC, KP_CONF,
                    POSE_CLASS)
from tracker import box_center

SIDE_KO = {"left": "왼쪽", "right": "오른쪽", "both": "양쪽"}
MIRROR = {"left": "right", "right": "left"}
L_SH, R_SH = 5, 6


def falling(track):
    """떨어지는 중이면 (drop, speed), 아니면 None."""
    if track.name != FALL_CLASS or len(track.trail) < 2:
        return None
    drop, speed = track.motion(FALL_WINDOW_SEC)
    if drop >= FALL_MIN_DROP and speed >= FALL_MIN_SPEED:
        return drop, speed
    return None


def body_center_x(person):
    """몸의 가로 중심. 어깨가 보이면 어깨 중점을 쓴다 — 팔을 뻗으면 박스 중심이 따라 움직인다."""
    po = person.pose
    if po:
        xs = [po["kp"][j][0] for j in (L_SH, R_SH) if po["kp"][j][2] >= KP_CONF]
        if xs:
            return sum(xs) / len(xs)
    return box_center(person.box)[0]


def body_side(screen_side, axis):
    """화면 기준 좌/우 + 어깨 간격 평균 -> 사람 기준 좌/우. -> (side, basis)"""
    if screen_side == "both":
        return "both", "center"
    if axis is None:
        return screen_side, "screen"
    if axis <= -FALL_PROFILE_RATIO:
        return screen_side, "back"
    if axis >= FALL_PROFILE_RATIO:
        return MIRROR[screen_side], "front"
    return "both", "profile"


def judge(box, person):
    """떨어지는 박스 하나와 사람 한 명 -> 판정, 그 사람과 무관하면 None."""
    bx, by = box_center(box.box)
    px1, _, px2, py2 = person.box
    if by > py2:                              # 발밑보다 아래 — 사람 위로 떨어지는 게 아니다
        return None
    offset = (bx - body_center_x(person)) / max(px2 - px1, 1e-6)
    if abs(offset) > FALL_NEAR_RATIO:
        return None
    screen = ("both" if abs(offset) <= FALL_CENTER_RATIO else
              "right" if offset > 0 else "left")
    axis = person.body_axis()
    side, basis = body_side(screen, axis)
    return {"side": side, "screen_side": screen, "basis": basis,
            "facing": person.facing(), "body_axis": None if axis is None else round(axis, 2),
            "offset": round(offset, 2)}


def detect(tracks):
    """이번 추론에서 새로 잡힌 낙하 -> [(박스 트랙, 사람 트랙, 판정)].

    박스 트랙마다 한 번만 낸다. 떨어지는 동안 매 추론마다 조건을 만족하기 때문이다.
    가까운 사람이 없으면 아직 내지 않는다 — 떨어지면서 사람 쪽으로 올 수 있다.
    """
    people = [t for t in tracks if t.name == POSE_CLASS]
    found = []
    for box in tracks:
        box.falling = bool(falling(box))
        if not box.falling or box.fall_fired:
            continue
        best = None
        for person in people:
            j = judge(box, person)
            if j and (best is None or abs(j["offset"]) < abs(best[1]["offset"])):
                best = (person, j)
        if best:
            box.fall_fired = True
            drop, speed = box.motion(FALL_WINDOW_SEC)
            best[1].update(drop=round(drop, 3), speed=round(speed, 3))
            found.append((box, *best))
    return found
