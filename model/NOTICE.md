# 동봉된 모델에 대하여

`yolov8n.onnx` 는 바로 돌려볼 수 있게 넣어둔 데모용 가중치다.
`setup.py` 가 하는 것과 같은 과정으로 만들었다.

- 원본: [Ultralytics YOLOv8n](https://github.com/ultralytics/assets) (`yolov8n.pt`)
- 변환: `imgsz=640`, `opset=12`, `dynamic=False` 로 ONNX export
- 클래스: COCO 80종 (`names` 메타데이터에 들어 있고 `detector.py` 가 이를 읽는다)

## 라이선스

저장소 루트의 MIT 라이선스는 **이 프로젝트의 소스 코드에만** 적용된다.
`yolov8n.onnx` 는 Ultralytics 가중치에서 파생된 파일이라 원저작물의 라이선스인
**AGPL-3.0** 을 따른다. MIT로 재배포되는 것이 아니다.

AGPL이 곤란한 용도(코드 공개 없이 서비스에 얹는 등)라면 두 가지 방법이 있다.

- Ultralytics 상용 라이선스를 별도로 취득한다.
- 이 파일을 지우고 자체 학습 모델이나 라이선스가 맞는 모델을 `model/` 에 둔다.
  `detector.py` 는 클래스 이름을 하드코딩하지 않고 ONNX 메타데이터에서 읽으므로
  모델만 바꾸면 코드는 손댈 필요가 없다.
