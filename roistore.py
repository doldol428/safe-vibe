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
    """ray casting. vunexai-frontend canvas.ts 의 isPointInPolygon 과 동일."""
    inside = False
    n = len(points)
    for i in range(n):
        j = (i - 1) % n
        xi, yi = points[i]["x"], points[i]["y"]
        xj, yj = points[j]["x"], points[j]["y"]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside
