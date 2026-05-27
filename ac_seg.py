import cv2
from ultralytics import YOLO

# Paths
img_path = r"D:\Periodontal Bone Loss\pic\1.jpg"
model_path = r"D:\Periodontal Bone Loss\model\best_yolo11x_seg.pt"

# Load model
model = YOLO(model_path)

# Predict
results = model.predict(source=img_path, conf=0.25)

# Get overlay image
overlay = results[0].plot(labels=False, conf=False, boxes=False)

# Save result
output_path = r"D:\Periodontal Bone Loss\pic\ac_seg_overlay.jpg"
cv2.imwrite(output_path, overlay)

print("Saved:", output_path)