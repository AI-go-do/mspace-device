"""YOLO-World 모델에 우리 물건 이름을 넣어 젯슨용 ONNX 파일로 만든다 (맥이나 인터넷 되는 컴퓨터에서 한 번).

    python3 -m venv ~/world-venv && ~/world-venv/bin/pip install ultralytics clip-anytorch "setuptools<81"
    ~/world-venv/bin/python tools/export_world.py
    scp yolov8s-worldv2.onnx yolov8s-worldv2.names.json suyeon@<젯슨 IP>:~/mspace-device/gateway/

젯슨에서는 LABEL_MODEL=gateway/yolov8s-worldv2.onnx 로 켠다 (책상 사진 비교에서 달라진 곳에 이름 붙이기).
이름을 바꾸면 이 파일의 NAMES만 고치고 다시 만든다. 이름은 영어로 (모델이 영어 글로 배웠음).
room_camera.py의 TRASH·BELONGINGS·KOREAN에도 같은 이름을 넣어야 치울 것/두고 간 물건이 나뉜다.
"""
import json

from ultralytics import YOLOWorld

NAMES = ["cell phone", "laptop", "backpack", "handbag", "wallet", "credit card", "power bank", "earphone case",
         "tumbler", "pencil case", "book", "glasses", "umbrella", "remote control",
         "paper", "cup", "bottle", "can", "snack bag", "plastic bag", "tissue", "food container"]

model = YOLOWorld("yolov8s-worldv2.pt")  # 처음 한 번 자동으로 받음 (약 25MB)
model.set_classes(NAMES)
model.export(format="onnx", opset=12, simplify=True, imgsz=640)
with open("yolov8s-worldv2.names.json", "w", encoding="utf-8") as f:
    json.dump(NAMES, f, ensure_ascii=False)
print("만들었어요: yolov8s-worldv2.onnx, yolov8s-worldv2.names.json")
