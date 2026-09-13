"""YOLOv8 을 학습하고 safe-vibe 가 바로 쓸 수 있는 ONNX 로 내보낸다.

    python training/train.py --data training/dataset/data.yaml --model yolov8n

prepare_dataset.py 가 만든 data.yaml 을 받아서 학습하고, 끝나면 ONNX 로 변환해
model/ 에 넣는다. app.py 를 다시 띄우면 새 모델로 검출한다.

모델 크기:
    yolov8n   가볍고 빠르다. 라즈베리파이 CPU 에서 돌리려면 이쪽.
    yolov8m   정확하지만 무겁다. Pi 에서는 느려서 학습/검증용으로만 쓴다.

주의 — 이 스크립트는 app.py 와 같은 venv 에서 돌리지 말 것.
ultralytics 가 opencv-python(full) 을 끌고 오는데, 앱은 headless 를 쓴다.
같은 venv 에 둘 다 들어가면 cv2 가 서로 덮어써서 앱이 깨진다.
(requirements.txt 의 설명 참고)

    python -m venv .venv-train
    .venv-train/bin/pip install -r training/requirements.txt
    .venv-train/bin/python training/train.py ...
"""
import argparse
import shutil
from pathlib import Path

BASE = Path(__file__).resolve().parent
PROJECT = BASE.parent

# safe-vibe 의 detector.py 가 입력 shape 를 [1,3,H,W] 로 읽으므로 동적 축 없이
# 640 고정으로 낸다. opset 12 는 구형 onnxruntime(Pi 의 apt 버전 포함)까지
# 무난히 먹는 하한선이다. setup.py 가 기본 모델을 만들 때와 같은 조건이다.
IMGSZ, OPSET, DYNAMIC = 640, 12, False


def main():
    ap = argparse.ArgumentParser(description="YOLOv8 학습 + ONNX export")
    ap.add_argument("--data", required=True, help="prepare_dataset.py 가 만든 data.yaml")
    ap.add_argument("--model", default="yolov8n",
                    help="yolov8n | yolov8m | 이어서 학습할 .pt 경로")
    ap.add_argument("--epochs", type=int, default=50)
    ap.add_argument("--imgsz", type=int, default=IMGSZ, help="학습 해상도")
    ap.add_argument("--batch", type=int, default=16,
                    help="GPU 메모리가 부족하면 줄인다. -1 이면 자동")
    ap.add_argument("--device", default=None,
                    help="'0' = 첫 GPU, 'cpu' = CPU. 생략하면 자동 선택")
    ap.add_argument("--name", default="safe-vibe", help="결과 폴더 이름")
    ap.add_argument("--no-export", action="store_true", help="학습만 하고 ONNX 로 내보내지 않는다")
    args = ap.parse_args()

    # import 를 함수 안에 둔 이유: --help 만 보려는 사람이 무거운 torch 로딩을
    # 기다리지 않아도 되고, 패키지가 없을 때 설치 안내를 먼저 띄울 수 있다.
    try:
        from ultralytics import YOLO
    except ImportError:
        raise SystemExit(
            "ultralytics 가 없습니다. 학습 전용 venv 를 만들어 설치하세요:\n"
            "  python -m venv .venv-train\n"
            "  .venv-train/bin/pip install -r training/requirements.txt")

    data = Path(args.data).expanduser().resolve()
    if not data.is_file():
        raise SystemExit(f"data.yaml 이 없습니다: {data}\n"
                         f"  먼저 실행: python training/prepare_dataset.py --input <데이터셋>")

    # 이름만 준 경우 ultralytics 가 사전학습 가중치를 알아서 받는다.
    # 처음부터 학습하는 것보다 훨씬 적은 데이터로 수렴한다(전이학습).
    weights = args.model if args.model.endswith(".pt") else f"{args.model}.pt"
    print(f"[1/3] 모델 준비: {weights}")
    model = YOLO(weights)

    print(f"[2/3] 학습  epochs={args.epochs} imgsz={args.imgsz} batch={args.batch}")
    model.train(
        data=str(data),
        epochs=args.epochs,
        imgsz=args.imgsz,
        batch=args.batch,
        device=args.device,
        project=str(BASE / "runs"),
        name=args.name,
        exist_ok=True,
    )

    best = Path(model.trainer.best)
    print(f"      가장 좋은 가중치: {best}")

    if args.no_export:
        return

    print(f"[3/3] ONNX export  imgsz={IMGSZ} opset={OPSET} dynamic={DYNAMIC}")
    # 학습 해상도와 무관하게 export 는 640 고정이다. detector.py 가 ONNX 의
    # 입력 shape 를 그대로 읽어 전처리 크기를 정하기 때문이다.
    onnx = Path(YOLO(str(best)).export(
        format="onnx", imgsz=IMGSZ, opset=OPSET, dynamic=DYNAMIC, simplify=True))

    # model/ 에는 .onnx 를 하나만 둔다는 규칙이라(detector.py 가 여러 개면
    # 거부한다) 기존 것을 밀어내고 넣는다. 원본은 runs/ 아래 그대로 남는다.
    model_dir = PROJECT / "model"
    model_dir.mkdir(exist_ok=True)
    for old in model_dir.glob("*.onnx"):
        backup = old.with_suffix(".onnx.bak")
        backup.unlink(missing_ok=True)
        old.rename(backup)
        print(f"      기존 모델 보관: {backup.name}")
        # model/ 에서 저장소에 커밋된 onnx 는 yolov8n.onnx 하나뿐이다. 밀어내면
        # git 이 '삭제됨' 으로 잡는데, 없어진 게 아니라 자리를 비운 것이다.
        if old.name == "yolov8n.onnx":
            print("      (git 에 '삭제됨' 으로 뜨는 게 정상이다. "
                  "되돌리기: git checkout model/yolov8n.onnx)")
    dst = model_dir / f"{args.name}.onnx"
    shutil.copy2(onnx, dst)

    print()
    print(f"완료 -> {dst}")
    print("  클래스 이름은 ONNX 메타데이터에 들어가므로 detector.py 를 고칠 필요가 없다.")
    print("  실행:  .venv/bin/python app.py")


if __name__ == "__main__":
    main()
