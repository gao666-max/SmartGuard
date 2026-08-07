"""
安全帽检测模块 — helmet_detector.py
用法：
    from helmet_detector import HelmetDetector
    detector = HelmetDetector()
    result = detector.detect(frame)  # frame 是 numpy 数组 (BGR)
    print(result)  # {"total": 5, "with_helmet": 3, "without_helmet": 2}
"""

import os
import cv2
import torch
from ultralytics import YOLO

# 模型路径：优先用增强版模型（13类），回退到基础安全帽模型
_MODEL_PATH = os.path.join(os.path.dirname(__file__), "safetyvision_model.pt")
if not os.path.exists(_MODEL_PATH):
    _MODEL_PATH = os.path.join(os.path.dirname(__file__), "helmet_model.pt")
if not os.path.exists(_MODEL_PATH):
    _MODEL_PATH = "yolov8n.pt"


class HelmetDetector:
    def __init__(self, model_path=None):
        if model_path is None:
            model_path = _MODEL_PATH

        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = YOLO(model_path)
        self.model.to(self.device)
        self.class_names = self.model.names
        print(f"✅ 安全帽检测模型已加载 (设备: {self.device})")
        print(f"   类别: {self.class_names}")

    def detect(self, frame):
        """对单帧图像进行检测（新模型13类：Person/Hardhat/NO-Hardhat等）"""
        results = self.model(frame, conf=0.4, iou=0.5, verbose=False)[0]

        detections = []
        person_count = 0
        with_helmet = 0
        without_helmet = 0

        # Person class is class 11 in safetyvision
        has_person_class = "Person" in self.class_names.values()

        for box in results.boxes:
            cls_id = int(box.cls[0])
            cls_name = self.class_names[cls_id]
            confidence = float(box.conf[0])
            x1, y1, x2, y2 = box.xyxy[0].tolist()

            detections.append({
                "class": cls_name,
                "confidence": round(confidence, 2),
                "bbox": [int(x1), int(y1), int(x2), int(y2)]
            })

            if cls_name == "Person":
                person_count += 1
            elif cls_name in ("Hardhat", "hardhat", "helmet"):
                with_helmet += 1
            elif cls_name in ("NO-Hardhat", "no-hardhat", "no_helmet"):
                without_helmet += 1

        # 如果没有 Person 类，用 Hardhat + NO-Hardhat 之和作为总人数
        if has_person_class and person_count > 0:
            total = person_count
        else:
            total = with_helmet + without_helmet

        return {
            "total": total,
            "with_helmet": with_helmet,
            "without_helmet": without_helmet,
            "detections": detections
        }

    def draw(self, frame, result):
        """在画面上绘制检测框"""
        annotated = frame.copy()
        for det in result["detections"]:
            x1, y1, x2, y2 = det["bbox"]
            cls_name = det["class"]
            conf = det["confidence"]

            # 颜色编码：绿=佩戴, 红=未佩戴, 蓝=人员, 黄=其他PPE
            if cls_name in ("Hardhat", "hardhat", "helmet", "Safety Vest"):
                color = (0, 255, 0)    # 绿色 - 合规
            elif cls_name in ("NO-Hardhat", "no-hardhat", "no_helmet"):
                color = (0, 0, 255)    # 红色 - 违规
            elif cls_name == "Person":
                color = (255, 0, 0)    # 蓝色 - 仅识别到人
            else:
                color = (0, 255, 255)  # 黄色 - 其他

            label = f"{cls_name} {conf:.2f}"
            cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            cv2.putText(annotated, label, (x1, y1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        # 左上角统计
        text = f"佩戴: {result['with_helmet']} | 未佩戴: {result['without_helmet']}"
        cv2.putText(annotated, text, (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)

        return annotated


# ========== 测试：摄像头实时检测 ==========
if __name__ == "__main__":
    detector = HelmetDetector()

    # 尝试打开摄像头，Windows 用 DirectShow 后端
    cap = None
    for idx in [0, 1]:
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
        if cap.isOpened():
            print(f"📷 摄像头 {idx} 已打开")
            break
        cap.release()
        cap = None

    if cap is None:
        print("❌ 摄像头不可用，改试静态图片...")
        # 下载一张工地安全帽图片测试
        import urllib.request
        import numpy as np
        test_url = "https://img.alicdn.com/imgextra/i4/6000000007694/O1CN01wMGHrZ26vqGxbSX9L_!!6000000007694-0-tbvideo.jpg"
        try:
            req = urllib.request.urlopen(test_url, timeout=10)
            img_array = np.asarray(bytearray(req.read()), dtype=np.uint8)
            frame = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
            print(f"✅ 已加载测试图片，尺寸: {frame.shape}")
        except Exception:
            frame = np.zeros((480, 640, 3), dtype=np.uint8)

        result = detector.detect(frame)
        annotated = detector.draw(frame, result)
        print(f"检测结果: 共{result['total']}人, "
              f"佩戴{result['with_helmet']}人, 未佩戴{result['without_helmet']}人")
        cv2.imshow("Helmet Detection - press any key", annotated)
        cv2.waitKey(0)
        cv2.destroyAllWindows()
        exit()

    WINDOW_NAME = "Helmet Detection"
    print("📷 摄像头已打开，按 Q 或 ESC 退出...")
    print("(如果按键不响应，在终端按 Ctrl+C 强制退出)")

    try:
        while True:
            ret, frame = cap.read()
            if not ret:
                print("⚠ 读取帧失败，重试...")
                break

            result = detector.detect(frame)
            annotated = detector.draw(frame, result)

            cv2.imshow(WINDOW_NAME, annotated)
            key = cv2.waitKey(30)  # 30ms，别用 1ms
            if key != -1:
                key = key & 0xFF
                if key in (ord("q"), ord("Q"), 27):
                    print("退出中...")
                    break
    except KeyboardInterrupt:
        print("\nCtrl+C 退出")

    cap.release()
    cv2.destroyAllWindows()
    print("已退出")
