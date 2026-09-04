"""ROI(검출 영역) 저장소. RDBMS 없이 roi.json 파일 하나로 관리한다.

좌표는 0~1 정규화라 해상도가 바뀌어도 그대로 쓸 수 있다.
"""
import json
import os
import threading

from config import (DEFAULT_ROI_NAME, MAX_NAME, MAX_POINTS, MAX_ROI, ROI_FILE)

_lock = threading.Lock()


# ---------------------------------------------------------------- 정규화

def _clean_points(raw):
    """좌표 배열을 0~1 범위로 맞춘다. 형식이 틀린 점은 버린다."""
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
    return {
        "id": roi_id,
        "name": str(raw.get("name") or DEFAULT_ROI_NAME)[:MAX_NAME],
        "enabled": bool(raw.get("enabled", True)),
        "points": _clean_points(raw.get("points")),
    }


# ---------------------------------------------------------------- 파일 I/O

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


# ---------------------------------------------------------------- CRUD

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


# ---------------------------------------------------------------- 판정

def point_in_polygon(x, y, points):
    """ray casting 방식 다각형 내부 판정.

    index.html 의 inPolygon() 과 같은 알고리즘이어야 한다. 서버 판정과 화면
    표시가 갈리면 "박스는 안에 있는데 이벤트는 안 뜬다"가 된다.
    """
    inside = False
    n = len(points)
    for i in range(n):
        j = (i - 1) % n
        xi, yi = points[i]["x"], points[i]["y"]
        xj, yj = points[j]["x"], points[j]["y"]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def box_overlap_ratio(box, points):
    """박스와 다각형이 겹치는 면적이 박스 면적의 몇 배인지 (0~1).

    다각형을 박스의 네 변으로 차례로 잘라내고(Sutherland-Hodgman) 남은 조각의
    면적을 신발끈 공식으로 잰다. 잘라내는 쪽이 사각형(볼록)이라 ROI 가 오목해도
    면적은 맞다. 박스와 ROI 모두 0~1 정규화 좌표라 비율은 화면 비율과 무관하다.
    """
    x1, y1, x2, y2 = box
    area = (x2 - x1) * (y2 - y1)
    if area <= 0 or len(points) < 3:
        return 0.0

    poly = [(p["x"], p["y"]) for p in points]
    # (축, 경계값, 경계보다 큰 쪽이 안쪽인가): x>=x1, x<=x2, y>=y1, y<=y2
    for axis, bound, keep_greater in ((0, x1, True), (0, x2, False),
                                      (1, y1, True), (1, y2, False)):
        if not poly:
            return 0.0
        clipped = []
        prev = poly[-1]
        prev_in = prev[axis] >= bound if keep_greater else prev[axis] <= bound
        for cur in poly:
            cur_in = cur[axis] >= bound if keep_greater else cur[axis] <= bound
            if cur_in != prev_in:           # 경계를 가로지르는 변 -> 교점을 넣는다
                t = (bound - prev[axis]) / (cur[axis] - prev[axis])
                clipped.append((prev[0] + t * (cur[0] - prev[0]),
                                prev[1] + t * (cur[1] - prev[1])))
            if cur_in:
                clipped.append(cur)
            prev, prev_in = cur, cur_in
        poly = clipped

    inter = 0.0
    for i in range(len(poly)):
        ax, ay = poly[i]
        bx, by = poly[(i + 1) % len(poly)]
        inter += ax * by - bx * ay
    return min(abs(inter) / 2 / area, 1.0)
