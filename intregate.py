"""
PerioDx — Flask backend
Returns:
  - teeth_overlay   : base64 PNG — tooth segmentation
  - cej_overlay     : base64 PNG — CEJ detection
  - ac_overlay      : base64 PNG — AC segmentation
  - annotated_img   : base64 PNG — full bone-loss quantification (axes, CEJ/AC points, % labels)
  - tooth_bone_loss : list of {jaw, tooth_id, pct} for per-tooth map
"""

import cv2
import numpy as np
import base64
import json
from flask import Flask, request, jsonify
from ultralytics import YOLO
from shapely.geometry import Polygon, LineString, MultiLineString

app = Flask(__name__)

# ── Load models once at startup ──────────────────────────────────────────────
model_teeth = YOLO("model/teeth_seg.pt")
model_cej   = YOLO("model/cej_p.pt")
model_ac    = YOLO("model/best_yolo11x_seg.pt")


# ── Helpers ───────────────────────────────────────────────────────────────────

def img_to_b64(img_bgr: np.ndarray) -> str:
    """Encode a BGR numpy image to a base64 data-URI string."""
    _, buf = cv2.imencode(".png", img_bgr)
    return "data:image/png;base64," + base64.b64encode(buf).decode()


def extend_line(p1, p2, factor=0.4):
    p1, p2 = np.array(p1), np.array(p2)
    d = p2 - p1
    return tuple(p1 - d * factor), tuple(p2 + d * factor)


def extract_points(geom):
    if geom.is_empty:
        return []
    if geom.geom_type == "Point":
        return [geom]
    if geom.geom_type == "MultiPoint":
        return list(geom.geoms)
    if geom.geom_type == "GeometryCollection":
        return [g for g in geom.geoms if g.geom_type == "Point"]
    return []


def get_boundaries(masks_xy):
    lines = []
    if masks_xy is not None:
        for pts in masks_xy:
            if len(pts) >= 3:
                lines.append(Polygon(pts).boundary)
    return MultiLineString(lines)


def make_overlay(orig: np.ndarray, masks_xy, color_bgr: tuple, alpha=0.35) -> np.ndarray:
    """Draw semi-transparent filled masks + contours on a copy of orig."""
    out = orig.copy()
    canvas = orig.copy()
    if masks_xy:
        for pts in masks_xy:
            pts_int = np.int32(pts)
            cv2.fillPoly(canvas, [pts_int], color_bgr)
            cv2.drawContours(canvas, [pts_int], -1, color_bgr, 2)
    return cv2.addWeighted(canvas, alpha, out, 1 - alpha, 0)


def classify_tooth_x(teeth_polygons):
    """
    Split teeth into upper / lower jaw by vertical position of bbox centre.
    Returns sorted list of (centre_x, centre_y, polygon) for each jaw.
    """
    items = []
    for poly in teeth_polygons:
        pts = np.array(poly)
        cx = float(pts[:, 0].mean())
        cy = float(pts[:, 1].mean())
        items.append((cx, cy, poly))

    if not items:
        return [], []

    ys = [cy for _, cy, _ in items]
    mid_y = (min(ys) + max(ys)) / 2

    upper = sorted([(cx, cy, p) for cx, cy, p in items if cy < mid_y], key=lambda x: x[0])
    lower = sorted([(cx, cy, p) for cx, cy, p in items if cy >= mid_y], key=lambda x: x[0])
    return upper, lower


# ── Main route ────────────────────────────────────────────────────────────────

@app.route("/analyze", methods=["POST"])
def analyze():
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "No image uploaded"}), 400

    # Decode image
    buf = np.frombuffer(f.read(), np.uint8)
    orig = cv2.imdecode(buf, cv2.IMREAD_COLOR)
    if orig is None:
        return jsonify({"error": "Could not decode image"}), 400

    # ── Run models ────────────────────────────────────────────────────────────
    res_teeth = model_teeth(orig)[0]
    res_cej   = model_cej(orig)[0]
    res_ac    = model_ac(orig)[0]

    masks_teeth = res_teeth.masks.xy if res_teeth.masks else []
    masks_cej   = res_cej.masks.xy   if res_cej.masks   else []
    masks_ac    = res_ac.masks.xy    if res_ac.masks     else []

    # ── Stage overlays ────────────────────────────────────────────────────────
    # Stage 1 — teeth (multi-colour)
    teeth_canvas = orig.copy()
    teeth_blend  = orig.copy()
    colors_pool  = [
        (59,130,246),(139,92,246),(236,72,153),(245,158,11),(16,185,129),
        (6,182,212),(249,115,22),(132,204,26),(99,102,241),(20,184,166),
    ]
    for i, pts in enumerate(masks_teeth):
        col = colors_pool[i % len(colors_pool)]
        cv2.fillPoly(teeth_blend, [np.int32(pts)], col)
        cv2.drawContours(teeth_blend, [np.int32(pts)], -1, col, 2)
    teeth_overlay_img = cv2.addWeighted(teeth_blend, 0.4, teeth_canvas, 0.6, 0)

    # Stage 2 — CEJ (blue)
    cej_overlay_img = make_overlay(orig, masks_cej, (255, 80, 80))

    # Stage 3 — AC (yellow-cyan)
    ac_overlay_img = make_overlay(orig, masks_ac, (0, 220, 220))

    # ── Stage 4 — Bone-loss quantification ───────────────────────────────────
    cej_boundaries = get_boundaries(masks_cej)
    ac_boundaries  = get_boundaries(masks_ac)

    annotated = orig.copy()
    overlay   = orig.copy()

    # Draw CEJ/AC contours on annotation canvas
    for pts in masks_cej:
        cv2.drawContours(overlay, [np.int32(pts)], -1, (255, 60, 60), 2)
    for pts in masks_ac:
        cv2.drawContours(overlay, [np.int32(pts)], -1, (0, 230, 230), 2)

    tooth_bone_loss = []   # {jaw, tooth_id, pct}
    upper_teeth, lower_teeth = classify_tooth_x(masks_teeth)

    def process_jaw(jaw_items, jaw_label):
        for idx, (cx, cy, polygon) in enumerate(jaw_items):
            tooth_id = f"{jaw_label[0].upper()}{idx+1}"
            pts = np.array(polygon, dtype=np.int32)

            rect  = cv2.minAreaRect(pts)
            box   = cv2.boxPoints(rect)
            box   = np.int32(box)

            d01 = np.linalg.norm(box[0] - box[1])
            d12 = np.linalg.norm(box[1] - box[2])
            if d01 < d12:
                pt1 = (box[0] + box[1]) / 2
                pt2 = (box[2] + box[3]) / 2
            else:
                pt1 = (box[1] + box[2]) / 2
                pt2 = (box[3] + box[0]) / 2

            # Draw axis (green)
            cv2.line(annotated,
                     tuple(np.int32(pt1)), tuple(np.int32(pt2)),
                     (0, 220, 0), 2)

            ext_p1, ext_p2 = extend_line(pt1, pt2, factor=0.1)
            axis_line = LineString([ext_p1, ext_p2])

            cej_ints = extract_points(axis_line.intersection(cej_boundaries))
            ac_ints  = extract_points(axis_line.intersection(ac_boundaries))

            if not (cej_ints and ac_ints):
                tooth_bone_loss.append({"jaw": jaw_label, "tooth_id": tooth_id, "pct": None})
                continue

            min_dist, best_cej, best_ac = float("inf"), None, None
            for c in cej_ints:
                cv2.circle(annotated, (int(c.x), int(c.y)), 4, (0, 0, 255), -1)
            for a in ac_ints:
                cv2.circle(annotated, (int(a.x), int(a.y)), 4, (255, 0, 255), -1)
            for c in cej_ints:
                for a in ac_ints:
                    d = np.linalg.norm([c.x - a.x, c.y - a.y])
                    if d < min_dist:
                        min_dist, best_cej, best_ac = d, c, a

            if best_cej is None:
                tooth_bone_loss.append({"jaw": jaw_label, "tooth_id": tooth_id, "pct": None})
                continue

            # Draw CEJ–AC distance line (white)
            cv2.line(annotated,
                     (int(best_ac.x), int(best_ac.y)),
                     (int(best_cej.x), int(best_cej.y)),
                     (255, 255, 255), 2)

            # Find root tip (directional dot product)
            vac_x = best_ac.x - best_cej.x
            vac_y = best_ac.y - best_cej.y
            dot1  = vac_x*(pt1[0]-best_cej.x) + vac_y*(pt1[1]-best_cej.y)
            final_bottom = pt1 if dot1 > 0 else pt2

            # Yellow root-tip dot
            cv2.circle(annotated,
                       (int(final_bottom[0]), int(final_bottom[1])),
                       5, (0, 255, 255), -1)

            dist_cej_root = np.linalg.norm([best_cej.x - final_bottom[0],
                                            best_cej.y - final_bottom[1]])
            if dist_cej_root > 0:
                pct_raw = (min_dist / dist_cej_root) * 100
                pct_val = max(0.0, pct_raw - 15)   # subtract 2 mm bio-width proxy
            else:
                pct_val = 0.0

            # Annotate percentage
            tx = int((pt1[0] + pt2[0]) / 2) - 24
            ty = int((pt1[1] + pt2[1]) / 2) - 10
            cv2.putText(annotated, f"{pct_val:.1f}%", (tx, ty),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2, cv2.LINE_AA)

            tooth_bone_loss.append({"jaw": jaw_label, "tooth_id": tooth_id, "pct": round(pct_val, 2)})

    process_jaw(upper_teeth, "upper")
    process_jaw(lower_teeth, "lower")

    # Blend overlay (CEJ/AC contours) onto annotated
    alpha = 0.3
    annotated_final = cv2.addWeighted(overlay, alpha, annotated, 1 - alpha, 0)

    return jsonify({
        "teeth_overlay":   img_to_b64(teeth_overlay_img),
        "cej_overlay":     img_to_b64(cej_overlay_img),
        "ac_overlay":      img_to_b64(ac_overlay_img),
        "annotated_img":   img_to_b64(annotated_final),
        "tooth_bone_loss": tooth_bone_loss,
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)