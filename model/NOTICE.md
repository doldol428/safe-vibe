# 동봉된 모델에 대하여

`yolov8n.onnx` 는 바로 돌려볼 수 있게 넣어둔 데모용 가중치다.
`setup.py` 가 하는 것과 같은 과정으로 만들었다.

- 원본: [Ultralytics YOLOv8n](https://github.com/ultralytics/assets) (`yolov8n.pt`)
- 변환: `imgsz=640`, `opset=12`, `dynamic=False` 로 ONNX export
- 클래스: COCO 80종 (`names` 메타데이터에 들어 있고 `detector.py` 가 이를 읽는다)

## 직접 학습한 모델 (`trained/`)

사무실 영상 3세션에서 뽑아 라벨링한 550장(box 7,906 · person 609)으로 `training/train.py` 를
돌려 만들었다. 둘 다 같은 데이터·분할·epoch(50)·batch(16)·seed 이고 증강만 다르다.

| 파일 | 증강 | 검증 mAP50 / 50-95 |
|---|---|---|
| `trained/v8n-noaug/safe-vibe-v8n-noaug.onnx` | ultralytics 기본값 | 0.956 / 0.815 |
| `trained/v8n-aug/safe-vibe-v8n-aug.onnx` | + `degrees=10` `shear=3` `perspective=0.0005` | 0.946 / 0.767 |

- 시작점: [Ultralytics YOLOv8n](https://github.com/ultralytics/assets) 사전학습 가중치 (`yolov8n.pt`)
- 변환: `imgsz=640`, `opset=12`, `dynamic=False`, `simplify=True`
- 클래스: `box`, `person` 2종
- 라이선스: 사전학습 가중치에서 이어 학습한 파생물이라 아래와 같이 **AGPL-3.0**

## 방향 분석용 pose 모델

`model/pose/yolov8n-pose.onnx` 는 저장소에 들어 있지 않다. README 의 '방향 분석' 절차로 만든다.

- 원본: [Ultralytics YOLOv8n-pose](https://github.com/ultralytics/assets) (`yolov8n-pose.pt`)
- 변환: 위와 같은 조건 (`imgsz=640`, `opset=12`, `dynamic=False`)
- 출력: 사람 1클래스 + COCO 관절점 17개 (`kpt_shape=[17, 3]`)
- 라이선스: 아래와 같이 **AGPL-3.0**

## 라이선스

저장소 루트의 MIT 라이선스는 **이 프로젝트의 소스 코드에만** 적용된다.
`yolov8n.onnx` 는 Ultralytics 가중치에서 파생된 파일이라 원저작물의 라이선스인
**AGPL-3.0** 을 따른다. MIT로 재배포되는 것이 아니다.

AGPL이 곤란한 용도(코드 공개 없이 서비스에 얹는 등)라면 두 가지 방법이 있다.

- Ultralytics 상용 라이선스를 별도로 취득한다.
- 이 파일을 지우고 자체 학습 모델이나 라이선스가 맞는 모델을 `model/` 에 둔다.
  `detector.py` 는 클래스 이름을 하드코딩하지 않고 ONNX 메타데이터에서 읽으므로
  모델만 바꾸면 코드는 손댈 필요가 없다.
