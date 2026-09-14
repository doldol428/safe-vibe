# 학습

라벨링된 데이터로 YOLOv8 을 학습해서 `model/` 의 ONNX 를 갈아끼운다.
앱(`app.py`)은 고칠 것이 없다 — 클래스 이름을 ONNX 메타데이터에서 읽기 때문이다.

```
COCO 라벨 (annotations.json + images/)
        │
        │  prepare_dataset.py   검증 → YOLO 포맷 변환 → train/val 분할
        ▼
   dataset/data.yaml
        │
        │  train.py             학습 → ONNX export
        ▼
   model/safe-vibe.onnx  ──→  app.py 가 그대로 사용
```

## 1. 데이터 준비

```bash
python training/prepare_dataset.py --input labeling_coco_dataset.zip
```

표준 라이브러리만 쓰므로 학습 환경을 만들기 전에 먼저 돌려볼 수 있다.
변환 없이 **확인만** 하려면:

```bash
python training/prepare_dataset.py --input labeling_coco_dataset.zip --check
```

무엇을 검증하나:

| 확인 | 왜 |
|---|---|
| image id / annotation id 중복 | 중복이면 라벨이 덮어써져 조용히 사라진다 |
| 존재하지 않는 image_id 참조 | 추출이 중간에 끊긴 신호 |
| json 에 있는데 없는 이미지 파일 | 학습 중에 터진다 |
| 정의되지 않은 category_id | 클래스 번호가 밀린다 |
| w/h 가 0 이하인 bbox | 학습이 NaN 으로 죽는다 |
| 이미지 경계를 벗어난 bbox | 가장자리 물체면 정상. 크게 벗어나면 좌표계 착오 |
| 객체가 없는 이미지 | 배경 이미지인지 라벨 누락인지 사람이 판단 |

## 2. 학습

앱과 **다른 venv** 를 쓴다. 이유는 `training/requirements.txt` 에 적어 뒀다.

```bash
python -m venv .venv-train
.venv-train/bin/pip install -r training/requirements.txt
.venv-train/bin/python training/train.py --data training/dataset/data.yaml --model yolov8n
```

| 옵션 | 기본값 | 설명 |
|---|---|---|
| `--model` | `yolov8n` | `yolov8n` 은 Pi 용, `yolov8m` 은 정확하지만 무겁다 |
| `--epochs` | `50` | 데이터가 적으면 늘리고, 과적합하면 줄인다 |
| `--batch` | `16` | GPU 메모리 부족하면 낮춘다. `-1` 이면 자동 |
| `--device` | 자동 | `0` = 첫 GPU, `cpu` = CPU |
| `--name` | `safe-vibe` | 결과 폴더(`training/runs/<name>`)와 ONNX 파일 이름 |
| `--degrees` | 0 | 회전 ±도. 기울어진 박스·카메라에 강해진다 (예: `10`) |
| `--shear` | 0 | 기울임 ±도 (예: `3`) |
| `--perspective` | 0 | 원근 왜곡 (예: `0.0005`, 0.001 을 넘기면 과하다) |
| `--mixup` | 0 | 두 사진을 겹칠 확률 (예: `0.1`) |

끝나면 `model/safe-vibe.onnx` 가 생기고 기존 모델은 `.onnx.bak` 으로 남는다.

### 증강 전후 비교

증강 옵션 없이 학습한 결과는 `training/runs/safe-vibe-v8n-noaug/` 에 비교 기준으로 남겨 뒀다.
데이터·분할·epoch·batch·seed 는 같게 두고 증강만 바꿔야 차이를 증강 탓으로 읽을 수 있다.

```bash
.venv-train/bin/python training/train.py --data training/dataset/data.yaml --model yolov8n \
    --degrees 10 --shear 3 --perspective 0.0005
```

증강 전 모델로 앱을 띄우려면 그 폴더의 ONNX 를 가리킨다 (폴더에 onnx 가 하나뿐이라 그대로 읽힌다).

```bash
MODEL_DIR=training/runs/safe-vibe-v8n-noaug/weights .venv/bin/python app.py
```

**기본 증강은 박스를 기울이지 않는다.** ultralytics 기본값은 모자이크·좌우 반전·밝기·이동·크기만
켜고 회전(`degrees`)·기울임(`shear`)·원근(`perspective`)은 0 이다. 회전을 너무 크게 주면
돌아간 박스를 감싸는 더 큰 직사각형이 정답이 되어 박스가 헐거워지므로 작게 시작한다.

```bash
.venv/bin/python app.py
```

## 알아둘 것

**train/val 을 무작위로 나누지 않는다.** 이 데이터는 영상에서 뽑은 연속
프레임이라 이웃 프레임이 거의 같은 그림이다. 무작위로 섞으면 val 에 train 과
사실상 같은 장면이 들어가 점수가 실제보다 좋게 나온다. 그래서 세션별로 뒤쪽
구간을 통째로 val 로 뗀다.

**ONNX 는 640 고정, opset 12, dynamic 없음으로 내보낸다.** `detector.py` 가
입력 shape 를 그대로 읽어 전처리 크기를 정하고, opset 12 는 Pi 의 구형
onnxruntime 까지 받아주는 하한선이다. 학습 해상도(`--imgsz`)와는 별개다.

**학습하면 `model/yolov8n.onnx` 가 없어진다.** `model/` 에는 ONNX 를 하나만
두는 규칙이라(`detector.py` 가 여러 개면 거부한다) 새 모델이 들어가면서 기준
모델이 `.onnx.bak` 으로 밀려난다. 저장소에 커밋된 파일이라 `git status` 에
`deleted` 로 뜨는데 정상이다. 되돌리려면 `git checkout model/yolov8n.onnx`.

**가중치 라이선스.** 사전학습 가중치에서 파생된 모델은 원저작물을 따라
AGPL-3.0 이다. 저장소의 MIT 는 소스 코드에만 적용된다 (`model/NOTICE.md`).
