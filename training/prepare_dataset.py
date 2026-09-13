"""라벨링된 COCO 데이터셋을 YOLO 학습용으로 변환한다.

    python training/prepare_dataset.py --input labeling_coco_dataset.zip

하는 일 (순서대로):
    1. 검증      라벨이 제대로 뽑혔는지 먼저 확인한다. 여기서 걸러야 학습을
                 몇 시간 돌린 뒤에 "데이터가 이상했다"를 알게 되는 일이 없다
    2. 변환      COCO(절대좌표 xywh) -> YOLO(정규화 cx cy w h)
    3. 분할      train / val
    4. data.yaml ultralytics가 읽는 진입점

외부 패키지를 쓰지 않는다. 표준 라이브러리만으로 돌아가므로 학습 환경을
만들기 전에 데이터부터 확인할 수 있다.

두 포맷의 차이가 이 스크립트의 전부다:

    COCO   bbox = [x, y, w, h]        좌상단 기준, 픽셀 단위, annotations.json 한 파일
    YOLO   cls cx cy w h              중심점 기준, 0~1 정규화, 이미지마다 .txt 한 개
"""
import argparse
import collections
import json
import random
import shutil
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- 검증

def verify(coco, root):
    """라벨이 제대로 뽑혔는지 확인하고 (치명적 문제, 경고) 를 돌려준다.

    치명적 문제가 하나라도 있으면 변환을 멈춘다. 경고는 학습을 막지는
    않지만 알고 넘어가야 하는 것들이다.
    """
    imgs, anns, cats = coco["images"], coco["annotations"], coco["categories"]
    fatal, warn = [], []

    ids = [i["id"] for i in imgs]
    if len(ids) != len(set(ids)):
        fatal.append(f"image id 중복 {len(ids) - len(set(ids))}건")

    known = {i["id"] for i in imgs}
    orphan = sum(1 for a in anns if a["image_id"] not in known)
    if orphan:
        fatal.append(f"존재하지 않는 image_id 를 가리키는 annotation {orphan}건")

    missing = [i["file_name"] for i in imgs if not (root / i["file_name"]).is_file()]
    if missing:
        fatal.append(f"json 에는 있으나 실제 파일이 없는 이미지 {len(missing)}건 "
                     f"(예: {missing[0]})")

    cat_ids = {c["id"] for c in cats}
    unknown = {a["category_id"] for a in anns} - cat_ids
    if unknown:
        fatal.append(f"categories 에 정의되지 않은 category_id {sorted(unknown)}")

    # --- 경고 ---
    per_img = collections.Counter(a["image_id"] for a in anns)
    empty = [i for i in imgs if per_img[i["id"]] == 0]
    if empty:
        # 배경만 있는 이미지도 학습에 쓸 수는 있다(오탐 억제에 도움). 다만
        # 의도한 것인지 라벨링이 빠진 것인지는 사람만 안다.
        warn.append(f"객체가 하나도 없는 이미지 {len(empty)}장 — 라벨 누락인지 확인")

    size = {i["id"]: (i["width"], i["height"]) for i in imgs}
    degenerate = clipped = 0
    max_over = 0.0
    for a in anns:
        x, y, w, h = a["bbox"]
        if w <= 0 or h <= 0:
            degenerate += 1
            continue
        W, H = size[a["image_id"]]
        over = max(0.0, -x, -y, (x + w) - W, (y + h) - H)
        if over > 1:
            clipped += 1
            max_over = max(max_over, over)
    if degenerate:
        fatal.append(f"너비/높이가 0 이하인 bbox {degenerate}건")
    if clipped:
        # 화면 가장자리에 걸친 물체는 원래 이렇게 나온다. 변환할 때 잘라내면
        # 되고, 오히려 여기서 크게 벗어난 값이 나오면 좌표계를 잘못 쓴 것이다.
        warn.append(f"이미지 경계를 벗어난 bbox {clipped}건 (최대 {max_over:.0f}px) "
                    f"— 변환 시 경계로 잘라낸다")

    return fatal, warn


def report(coco, root):
    """사람이 읽을 요약. 학습 전에 눈으로 한 번 훑으라고 있는 것이다."""
    imgs, anns = coco["images"], coco["annotations"]
    name = {c["id"]: c["name"] for c in coco["categories"]}
    per_img = collections.Counter(a["image_id"] for a in anns)
    counts = [per_img[i["id"]] for i in imgs]

    print(f"  이미지 {len(imgs)}장 · 객체 {len(anns)}개 · 클래스 {len(coco['categories'])}종")
    for cid, n in collections.Counter(a["category_id"] for a in anns).most_common():
        share = 100 * n / len(anns)
        print(f"    {name[cid]:12s} {n:6d}개 ({share:4.1f}%)")
    print(f"  이미지당 객체 수  최소 {min(counts)} / 평균 {sum(counts)/len(counts):.1f} / 최대 {max(counts)}")
    for res, n in collections.Counter((i["width"], i["height"]) for i in imgs).most_common(3):
        print(f"  해상도 {res[0]}x{res[1]}: {n}장")


# ---------------------------------------------------------------- 변환

def to_yolo(bbox, img_w, img_h):
    """COCO [x,y,w,h](픽셀) -> YOLO (cx,cy,w,h)(0~1). 경계를 벗어나면 자른다."""
    x, y, w, h = bbox
    # 먼저 이미지 안으로 자른다. 잘린 뒤의 중심이 진짜 중심이므로 순서가 중요하다.
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(float(img_w), x + w), min(float(img_h), y + h)
    if x1 <= x0 or y1 <= y0:
        return None
    return ((x0 + x1) / 2 / img_w, (y0 + y1) / 2 / img_h,
            (x1 - x0) / img_w, (y1 - y0) / img_h)


def split_images(imgs, val_ratio, seed):
    """train/val 로 나눈다. 같은 영상의 연속 프레임이 양쪽에 섞이지 않게 한다.

    이 데이터는 영상에서 뽑은 연속 프레임이라 이웃 프레임끼리 거의 같은
    그림이다. 그냥 무작위로 섞으면 val 에 train 과 사실상 같은 장면이 들어가
    성능이 실제보다 좋게 나온다(데이터 누수). 그래서 세션(project_id)별로
    뒤쪽 구간을 통째로 val 로 뗀다 — 시간상 나중 장면으로 평가하는 셈이다.

    project_id 가 없는 데이터셋이면 전체를 한 덩어리로 보고 같은 방식을 쓴다.
    """
    groups = collections.defaultdict(list)
    for i in imgs:
        groups[i.get("project_id", 0)].append(i)

    train, val = [], []
    for key in sorted(groups):
        frames = sorted(groups[key], key=lambda i: (i.get("frame_index", 0), i["id"]))
        cut = int(len(frames) * (1 - val_ratio))
        train += frames[:cut]
        val += frames[cut:]

    # 세션 단위로 잘라 놓으면 클래스가 한쪽에 몰릴 수 있다. 순서만 섞어
    # 학습 배치가 한 세션에 치우치지 않게 한다(구성은 그대로).
    rng = random.Random(seed)
    rng.shuffle(train)
    rng.shuffle(val)
    return train, val


def write_split(split_name, images, anns_by_img, cat_index, src_root, out_root, link):
    img_dir = out_root / "images" / split_name
    lbl_dir = out_root / "labels" / split_name
    img_dir.mkdir(parents=True, exist_ok=True)
    lbl_dir.mkdir(parents=True, exist_ok=True)

    boxes = dropped = 0
    for img in images:
        src = src_root / img["file_name"]
        dst = img_dir / Path(img["file_name"]).name
        if not dst.exists():
            if link:
                dst.symlink_to(src.resolve())
            else:
                shutil.copy2(src, dst)

        lines = []
        for a in anns_by_img.get(img["id"], []):
            box = to_yolo(a["bbox"], img["width"], img["height"])
            if box is None:
                dropped += 1
                continue
            cx, cy, w, h = box
            lines.append(f"{cat_index[a['category_id']]} {cx:.6f} {cy:.6f} {w:.6f} {h:.6f}")
        boxes += len(lines)
        # 객체가 없는 이미지는 빈 .txt 를 둔다. 파일이 아예 없으면 ultralytics 가
        # "라벨 없음"으로 경고하지만, 빈 파일은 "배경 이미지"라는 뜻이 된다.
        (lbl_dir / f"{Path(img['file_name']).stem}.txt").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    return boxes, dropped


def main():
    ap = argparse.ArgumentParser(description="COCO 라벨을 YOLO 학습용으로 변환")
    ap.add_argument("--input", required=True,
                    help="labeling_coco_dataset.zip 또는 풀어놓은 디렉터리")
    ap.add_argument("--output", default=str(BASE / "dataset"), help="출력 디렉터리")
    ap.add_argument("--val-ratio", type=float, default=0.2, help="검증 비율 (기본 0.2)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--link", action="store_true",
                    help="이미지를 복사하지 않고 심볼릭 링크 (용량 절약, 윈도우는 비권장)")
    ap.add_argument("--check", action="store_true", help="검증만 하고 변환하지 않는다")
    args = ap.parse_args()

    src = Path(args.input).expanduser().resolve()
    out = Path(args.output).expanduser().resolve()

    # zip 이면 풀어서 쓴다. 이미 푼 것이 있으면 다시 풀지 않는다(멱등).
    if src.is_file() and src.suffix == ".zip":
        work = out.parent / (src.stem.replace(" ", "_") + "_raw")
        if not (work / "annotations.json").is_file():
            print(f"[1/4] 압축 해제 -> {work}")
            work.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(src) as z:
                z.extractall(work)
        else:
            print(f"[1/4] 압축 해제 생략 (이미 있음): {work}")
        src = work
    else:
        print(f"[1/4] 입력 디렉터리: {src}")

    ann_file = src / "annotations.json"
    if not ann_file.is_file():
        raise SystemExit(f"annotations.json 이 없습니다: {ann_file}")
    coco = json.loads(ann_file.read_text(encoding="utf-8"))

    print("[2/4] 검증")
    report(coco, src)
    fatal, warn = verify(coco, src)
    for w in warn:
        print(f"  ! 경고: {w}")
    if fatal:
        for f in fatal:
            print(f"  X 문제: {f}")
        raise SystemExit("데이터에 치명적인 문제가 있어 변환을 멈춥니다.")
    print("  -> 통과")

    if args.check:
        return

    # COCO category_id 는 1부터이고 비어 있을 수도 있다. YOLO 는 0부터 빈틈없이
    # 이어지는 정수를 쓰므로 여기서 다시 번호를 매긴다. data.yaml 의 순서가
    # 곧 이 번호이므로 둘이 어긋나면 클래스가 통째로 뒤바뀐다.
    cats = sorted(coco["categories"], key=lambda c: c["id"])
    cat_index = {c["id"]: n for n, c in enumerate(cats)}
    names = [c["name"] for c in cats]

    anns_by_img = collections.defaultdict(list)
    for a in coco["annotations"]:
        anns_by_img[a["image_id"]].append(a)

    train, val = split_images(coco["images"], args.val_ratio, args.seed)
    print(f"[3/4] 분할  train {len(train)}장 / val {len(val)}장")

    if out.exists():
        shutil.rmtree(out)
    stats = {}
    for name, subset in (("train", train), ("val", val)):
        stats[name] = write_split(name, subset, anns_by_img, cat_index, src, out, args.link)
    for name, (boxes, dropped) in stats.items():
        msg = f"  {name}: 객체 {boxes}개"
        if dropped:
            msg += f" (잘라낸 뒤 사라진 bbox {dropped}개 제외)"
        print(msg)

    yaml = ["# ultralytics 가 읽는 데이터셋 정의.",
            "# names 의 순서가 라벨 .txt 의 클래스 번호와 1:1 로 대응한다.",
            f"path: {out}",
            "train: images/train",
            "val: images/val",
            "names:"]
    yaml += [f"  {n}: {name}" for n, name in enumerate(names)]
    (out / "data.yaml").write_text("\n".join(yaml) + "\n", encoding="utf-8")

    print(f"[4/4] 완료 -> {out / 'data.yaml'}")
    print(f"      클래스: {', '.join(f'{n}={x}' for n, x in enumerate(names))}")
    print()
    print("다음 단계:")
    print(f"  python training/train.py --data {out / 'data.yaml'} --model yolov8n")


if __name__ == "__main__":
    main()
