"""
dental_logic.py

Pure domain-logic functions pulled out of app.py:
  - FDI tooth-position mapping + related constants
  - periodontitis staging (compute_periodontitis_stage, build_report_notes)
  - bone-loss % computation (compute_tooth_bone_loss)
  - distilled per-tooth JSON builders for the SLM chat (build_tooth_report_json*)
  - report-cache disk I/O (load_cached_report)
  - SLM output sanity check (looks_degenerate)

None of these import Flask or touch `session` — they take plain values in and
return plain values out, so they're easy to unit test independent of the app.
"""

import os
import json
from flask import abort
from reportlab.platypus import Flowable
from reportlab.lib.enums import TA_CENTER

# ---------------------------------------------------------------------------
# Per-tooth position -> FDI tooth number mapping
#
# ORIENTATION CONFIRMED: source radiographs carry a "DROIT" (right) label
# positioned on the image's left side, which is the standard panoramic
# convention — the patient's right side is shown on the image's left,
# moving across to the patient's left side on the image's right. Since
# colab_server.py's build_teeth_map() sorts detected teeth by ascending
# x-coordinate within each jaw, position 1 (leftmost) is the patient's
# right-most tooth and position 16 (rightmost) is the patient's left-most
# tooth. That's exactly what the lists below encode:
#   upper: 18,17,16,15,14,13,12,11, 21,22,23,24,25,26,27,28
#   lower: 48,47,46,45,44,43,42,41, 31,32,33,34,35,36,37,38
#
# This still isn't true tooth identity — the models don't detect which
# specific tooth is which, only left-to-right position — so a missing or
# extra tooth anywhere in the arch will shift every FDI number after it by
# one slot. It assumes every uploaded radiograph carries the same DROIT/
# left-side-right orientation; if a scan ever comes in flipped, these
# numbers would be mirrored for that upload.
# ---------------------------------------------------------------------------
FDI_UPPER_ORDER = ["18", "17", "16", "15", "14", "13", "12", "11",
                    "21", "22", "23", "24", "25", "26", "27", "28"]

FDI_LOWER_ORDER = ["48", "47", "46", "45", "44", "43", "42", "41",
                    "31", "32", "33", "34", "35", "36", "37", "38"]


# ---------------------------------------------------------------------------
# FDI tooth number -> human-readable name, for the PDF report and anywhere
# else a clinician-facing tooth name is needed (as opposed to the raw FDI
# code like "16").
# ---------------------------------------------------------------------------
_FDI_BASE_NAMES = {
    "1": "Central Incisor",
    "2": "Lateral Incisor",
    "3": "Canine",
    "4": "First Premolar",
    "5": "Second Premolar",
    "6": "First Molar",
    "7": "Second Molar",
    "8": "Third Molar (Wisdom)",
}

_FDI_QUADRANT_LABELS = {
    "1": "Upper Right",
    "2": "Upper Left",
    "3": "Lower Left",
    "4": "Lower Right",
}

def fdi_tooth_name(tooth_number: str) -> str:
    """'16' -> 'Upper Right First Molar'. Falls back to the raw code if the
    FDI number is somehow outside the standard 11-48 permanent-teeth range."""
    if not tooth_number or len(tooth_number) != 2:
        return tooth_number or "Unknown"
    quadrant, position = tooth_number[0], tooth_number[1]
    quadrant_label = _FDI_QUADRANT_LABELS.get(quadrant)
    base_name = _FDI_BASE_NAMES.get(position)
    if not quadrant_label or not base_name:
        return tooth_number
    return f"{quadrant_label} {base_name}"


FDI_TOOTH_NAMES = {
    num: fdi_tooth_name(num)
    for num in FDI_UPPER_ORDER + FDI_LOWER_ORDER
}


# Third molars are routinely absent for reasons unrelated to periodontitis
# (never erupted, prophylactic extraction, impaction), so they're excluded
# when counting "teeth lost" for staging purposes.
THIRD_MOLARS = {"18", "28", "38", "48"}

# Recognized AAP/EFP 2018 complexity-factor keys a clinician can optionally
# tick at save time. Per the real criteria, ANY of these present pushes a
# case that would otherwise be Stage III up to Stage IV, regardless of RBL
# or tooth-loss count.
COMPLEXITY_FACTOR_KEYS = (
    "probing_depth_6mm_plus",   # PD >= 6mm at one or more sites
    "vertical_bone_defect_3mm_plus",
    "furcation_class_2_3",      # Class II or III furcation involvement
    "fewer_than_20_teeth",      # < 20 remaining teeth (excluding 3rd molars)
    "bite_collapse",            # drifting, flaring, secondary occlusal trauma
)

# Human-readable labels for the complexity factors, used when composing
# the saved report's notes text (see build_report_notes below).
CLINICAL_FINDING_LABELS = {
    "probing_depth_6mm_plus": "Probing depth \u22656mm at one or more sites",
    "vertical_bone_defect_3mm_plus": "Vertical bone defect \u22653mm",
    "furcation_class_2_3": "Furcation involvement (Class II/III)",
    "fewer_than_20_teeth": "Fewer than 20 remaining teeth",
    "bite_collapse": "Bite collapse / drifting / flaring",
}

# Cap on the free-text "notes" a clinician can type into the save modal,
# to keep the Report.notes TEXT column from being handed something
# unbounded.
CUSTOM_NOTES_MAX_LEN = 2000


def build_report_notes(complexity_factors, custom_notes):
    """
    Compose the Report.notes text from what the clinician actually chose
    at save time — the ticked complexity-factor checkboxes and any
    free-text they typed — instead of a fixed boilerplate sentence.

    complexity_factors: dict of COMPLEXITY_FACTOR_KEYS -> bool
    custom_notes: str, already trimmed/length-capped by the caller

    Returns a plain-text string suitable for Report.notes.
    """
    selected = [
        CLINICAL_FINDING_LABELS[key]
        for key in COMPLEXITY_FACTOR_KEYS
        if complexity_factors.get(key)
    ]

    parts = []
    if selected:
        parts.append(
            "Clinical findings noted at save time: " + ", ".join(selected) + "."
        )
    else:
        parts.append("No clinical findings were noted at save time.")

    if custom_notes:
        parts.append("Clinician notes: " + custom_notes)

    return " ".join(parts)


def compute_periodontitis_stage(teeth, complexity_factors=None):
    """
    Periodontitis staging approximated from the AAP/EFP 2018 criteria,
    using only what this pipeline can actually observe:

      - Radiographic bone loss % (RBL) per tooth, from the imaging model.
        This is the real RBL criterion, not a made-up substitute:
            < 15%   -> coronal third            (Stage I range)
            15-33%  -> coronal third, advanced   (Stage II range)
            >= 33%  -> extends to/beyond mid-root third (Stage III/IV range)

      - Tooth loss attributable to periodontitis, approximated by counting
        positions flagged "missing" in the per-tooth map (excluding third
        molars — see THIRD_MOLARS). This is the actual AAP/EFP criterion
        used to split Stage III (<=4 teeth lost) from Stage IV (>=5 teeth
        lost) — NOT an arbitrary higher bone-loss cutoff. Caveat: a tooth
        can be missing for reasons unrelated to periodontitis (trauma,
        orthodontic extraction, congenital absence), which this heuristic
        can't distinguish, so it will overestimate tooth loss in those cases.

    What this function still CANNOT determine, because it requires an
    in-person clinical exam rather than a radiograph:
      - Interdental clinical attachment loss (CAL) — the primary staging
        criterion in the real system.
      - Complexity factors (probing depth, vertical bone defects,
        furcation involvement, bite collapse, remaining-teeth count) —
        these can only be entered by a clinician, via `complexity_factors`.
        If provided and any factor is truthy, a case that would land in
        Stage III is escalated to Stage IV, matching the real staging
        rule that any complexity factor takes precedence over CAL/RBL/
        tooth-loss count.

    Returns one of: "Not Classified", "Stage I", "Stage II", "Stage III",
    "Stage IV".
    """
    if not teeth:
        return "Not Classified"

    max_pct = None
    missing_count = 0

    for jaw_key, order in (("upper", FDI_UPPER_ORDER), ("lower", FDI_LOWER_ORDER)):
        jaw_teeth = teeth.get(jaw_key) or []
        for tooth_data, tooth_number in zip(jaw_teeth, order):
            status = tooth_data.get("status")

            if status == "missing" and tooth_number not in THIRD_MOLARS:
                missing_count += 1
                continue

            pct = tooth_data.get("bone_loss_pct")
            if pct is None:
                continue
            try:
                pct = float(pct)
            except (TypeError, ValueError):
                continue
            if max_pct is None or pct > max_pct:
                max_pct = pct

    # No measurable bone loss on any present tooth.
    if max_pct is None or max_pct < 5:
        base_stage = "Not Classified"
    elif max_pct < 15:
        base_stage = "Stage I"
    elif max_pct < 33:
        base_stage = "Stage II"
    else:
        # RBL extends to/beyond the mid-root third -> Stage III/IV bracket.
        # Real criterion for the split: tooth loss count, not more bone loss.
        base_stage = "Stage IV" if missing_count >= 5 else "Stage III"

    # Complexity factors, if the clinician supplied any, can escalate
    # Stage III up to Stage IV regardless of the RBL/tooth-loss numbers.
    if base_stage == "Stage III" and complexity_factors:
        if any(complexity_factors.get(key) for key in COMPLEXITY_FACTOR_KEYS):
            base_stage = "Stage IV"

    return base_stage


def compute_tooth_bone_loss(cej_ac_distance_px, tooth_length_px):
    """
    Dynamically derive a tooth's bone-loss % + severity band from its two
    raw pixel measurements.

    IMPORTANT: this must exactly match the formula used at diagnosis time
    in colab_server.py's process_image():

        percentage_val = (cej_ac_distance_px / tooth_length_px) * 100
        percentage_val = max(0, percentage_val - 15)

    i.e. NOT a plain ratio — there's a flat 15-point offset subtracted
    (and floored at 0) before it's treated as a bone-loss %. Only the raw
    cej_ac_distance_px / tooth_length_px get persisted to the DB (see
    ToothMeasurement), not the derived percentage, so this recomputation
    has to reproduce that offset or saved-report percentages will drift
    from what was actually shown when the scan was first diagnosed.

    Nothing is cached — this runs fresh every time a saved report is
    opened in /records.

    Returns (bone_loss_pct: float | None, status: str) where status is
    one of "normal", "mild", "moderate", "severe", "unmeasured".
    """
    if cej_ac_distance_px is None or tooth_length_px is None:
        return None, "unmeasured"

    try:
        distance = float(cej_ac_distance_px)
        length = float(tooth_length_px)
    except (TypeError, ValueError):
        return None, "unmeasured"

    if length <= 0:
        return None, "unmeasured"

    pct = (distance / length) * 100
    pct = max(0.0, pct - 15)   # <-- matches colab_server.py's offset exactly
    pct = min(pct, 100.0)      # safety cap; colab_server.py has no upper bound
    pct = round(pct, 1)

    if pct < 15:
        status = "normal"
    elif pct < 33:
        status = "mild"
    elif pct < 50:
        status = "moderate"
    else:
        status = "severe"

    return pct, status


def build_tooth_report_json(cached_report):
    """
    Distills a cached diagnosis (report_cache/<uuid>.json) down to exactly
    what the SLM needs: per-tooth cej_ac_distance_px, tooth_length_px, and
    bone_loss_pct -- recomputed fresh via compute_tooth_bone_loss so it can
    never drift from what colab_server.py actually produced -- plus an
    overall stage estimate.

    Deliberately NOT the raw cached report: that also carries the four
    base64 report images, which are megabytes the model has no use for and
    would blow past its context window.
    """
    teeth = (cached_report or {}).get('teeth') or {}
    upper_in = teeth.get('upper') or []
    lower_in = teeth.get('lower') or []

    def build_jaw(jaw_teeth, order):
        jaw_out = []
        for tooth_data, tooth_number in zip(jaw_teeth, order):
            if tooth_data.get('status') == 'missing':
                jaw_out.append({
                    "tooth_number": tooth_number,
                    "status": "missing",
                    "cej_ac_distance_px": None,
                    "tooth_length_px": None,
                    "bone_loss_pct": None
                })
                continue

            cej_ac = tooth_data.get('cej_ac_distance_px')
            length = tooth_data.get('tooth_length_px')
            pct, status = compute_tooth_bone_loss(cej_ac, length)

            jaw_out.append({
                "tooth_number": tooth_number,
                "status": status,
                "cej_ac_distance_px": cej_ac,
                "tooth_length_px": length,
                "bone_loss_pct": pct
            })
        return jaw_out

    upper_out = build_jaw(upper_in, FDI_UPPER_ORDER)
    lower_out = build_jaw(lower_in, FDI_LOWER_ORDER)

    stage = compute_periodontitis_stage({"upper": upper_out, "lower": lower_out})

    return {
        "periodontitis_stage_estimate": stage,
        "teeth": {"upper": upper_out, "lower": lower_out}
    }


def build_tooth_report_json_from_saved(report):
    """
    Same shape as build_tooth_report_json(), but sourced from a *saved*
    Report/ToothMeasurement DB row (records.html) instead of the on-disk
    diagnosis cache. Percentages are recomputed live via
    compute_tooth_bone_loss so they can never drift from what's stored.
    """
    measurements_by_tooth = {m.tooth_number: m for m in report.tooth_measurements}

    def build_jaw(order):
        jaw = []
        for tooth_number in order:
            m = measurements_by_tooth.get(tooth_number)
            if m is None:
                jaw.append({
                    "tooth_number": tooth_number,
                    "status": "missing",
                    "cej_ac_distance_px": None,
                    "tooth_length_px": None,
                    "bone_loss_pct": None
                })
                continue
            pct, status = compute_tooth_bone_loss(m.cej_ac_distance_px, m.tooth_length_px)
            jaw.append({
                "tooth_number": tooth_number,
                "status": status,
                "cej_ac_distance_px": float(m.cej_ac_distance_px) if m.cej_ac_distance_px is not None else None,
                "tooth_length_px": float(m.tooth_length_px) if m.tooth_length_px is not None else None,
                "bone_loss_pct": pct
            })
        return jaw

    return {
        "periodontitis_stage_estimate": report.periodontitis_stage,
        "teeth": {
            "upper": build_jaw(FDI_UPPER_ORDER),
            "lower": build_jaw(FDI_LOWER_ORDER)
        }
    }


def load_cached_report(report_id, owner_user_id, reports_dir):
    """Fetch a cached report JSON from disk and confirm the caller owns it."""
    if not report_id:
        return None
    report_path = os.path.join(reports_dir, f"{report_id}.json")
    if not os.path.exists(report_path):
        return None
    with open(report_path, 'r') as f:
        cached_report = json.load(f)
    if cached_report.get('user_id') != owner_user_id:
        abort(403)
    return cached_report


def looks_degenerate(text, max_repeat=8):
    """
    Detects when the SLM has fallen into a repetition loop (e.g. a wall
    of "the the the ..."), a known failure mode for small quantized
    models when input gets too long or generation is under-constrained.
    Returns True if any run of `max_repeat` consecutive words is a
    single repeated word.
    """
    words = text.split()
    if len(words) < max_repeat:
        return False
    return any(
        len(set(words[i:i + max_repeat])) == 1
        for i in range(len(words) - max_repeat)
    )





# ---------------------------------------------------------------------------
# PDF report export (used by GET /api/reports/<id>/pdf in app.py)
# ---------------------------------------------------------------------------
from io import BytesIO
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable, Image
)


# Path to the PerioDx/ACE brand mark used in the PDF header. Adjust this
# to wherever the logo actually lives relative to this module (e.g. your
# Flask app's static/img folder).
LOGO_PATH = os.path.join(os.path.dirname(__file__), "static", "img", "periodx-logo.png")

_STAGE_COLORS = {
    "Stage I": colors.HexColor("#00a67e"),
    "Stage II": colors.HexColor("#c9880a"),
    "Stage III": colors.HexColor("#c65a34"),
    "Stage IV": colors.HexColor("#b03030"),
    "Not Classified": colors.HexColor("#6b6885"),
}

_STATUS_ROW_COLORS = {
    "severe": colors.HexColor("#fdeaea"),
    "moderate": colors.HexColor("#fdf0e8"),
    "mild": colors.HexColor("#fdf6e3"),
    "normal": colors.HexColor("#e8f9f3"),
}

_STATUS_LABELS = {
    "normal": "Normal",
    "mild": "Mild",
    "moderate": "Moderate",
    "severe": "Severe",
    "unmeasured": "Unmeasured",
    "missing": "Not detected",
}




class ToothMapRow(Flowable):
    """
    Draws one row of FDI tooth boxes — rounded, color-coded by bone-loss
    severity, each showing the FDI number on top and the bone-loss % (or a
    blank/greyed box for undetected teeth) below. Mirrors the .tm-tooth
    boxes in the web UI (dashboard_user.html / records.html).
    """
    BASE_BOX_W = 30
    BOX_H = 34
    BASE_GAP = 3

    _FILL = {
        "normal": colors.HexColor("#e8f9f3"),
        "mild": colors.HexColor("#fdf6e3"),
        "moderate": colors.HexColor("#fdf0e8"),
        "severe": colors.HexColor("#fdeaea"),
        "unmeasured": colors.HexColor("#f3f2f8"),
        "missing": colors.HexColor("#f7f6fb"),
    }
    _BORDER = {
        "normal": colors.HexColor("#00c896"),
        "mild": colors.HexColor("#f5a524"),
        "moderate": colors.HexColor("#ef7a54"),
        "severe": colors.HexColor("#d43d3d"),
        "unmeasured": colors.HexColor("#d8d5e8"),
        "missing": colors.HexColor("#e5e3f0"),
    }

    def __init__(self, jaw_teeth, width):
        Flowable.__init__(self)
        self.jaw_teeth = jaw_teeth
        self.width = width
        self.height = self.BOX_H

        n = max(len(jaw_teeth), 1)
        total_box_w = n * self.BASE_BOX_W + (n - 1) * self.BASE_GAP
        if total_box_w > width:
            scale = width / total_box_w
            self.box_w = self.BASE_BOX_W * scale
            self.gap = self.BASE_GAP * scale
        else:
            self.box_w = self.BASE_BOX_W
            self.gap = self.BASE_GAP

    def wrap(self, availWidth, availHeight):
        return (self.width, self.height)

    def draw(self):
        c = self.canv
        n = len(self.jaw_teeth)
        total_w = n * self.box_w + (n - 1) * self.gap
        x = (self.width - total_w) / 2.0  # center the row
        y = 0

        for t in self.jaw_teeth:
            status = t.get("status") or "missing"
            fill = self._FILL.get(status, self._FILL["missing"])
            border = self._BORDER.get(status, self._BORDER["missing"])

            c.setFillColor(fill)
            c.setStrokeColor(border)
            c.setLineWidth(1)
            c.roundRect(x, y, self.box_w, self.BOX_H, 4, fill=1, stroke=1)

            faded = colors.HexColor("#c7c4d9")
            num_color = faded if status == "missing" else colors.HexColor("#6b6885")
            c.setFillColor(num_color)
            c.setFont("Helvetica-Bold", 6.5)
            c.drawCentredString(x + self.box_w / 2, y + self.BOX_H - 12, str(t["tooth_number"]))

            pct = t.get("bone_loss_pct")
            if status == "missing":
                label = ""
            elif pct is not None:
                label = f"{pct}%"
            else:
                label = "—"
            pct_color = faded if status == "missing" else colors.HexColor("#211f36")
            c.setFillColor(pct_color)
            c.setFont("Helvetica-Bold", 7.5)
            c.drawCentredString(x + self.box_w / 2, y + 6, label)

            x += self.box_w + self.gap








def build_report_pdf(report, patient, teeth: dict, generated_by=None) -> BytesIO:
    """
    Builds a full diagnostic-report PDF for one saved Report row.

    report:  the Report model instance (id, report_date, periodontitis_stage, notes)
    patient: the Patient model instance (id, first_name, last_name, phone, diabetic)
    teeth:   {"upper": [...], "lower": [...]} in the same shape returned by
             GET /api/reports/<id> (tooth_number, status, bone_loss_pct)
    generated_by: the User model instance of the clinician downloading this
             report (first_name, last_name, email) — shown in the summary
             block as "Generated by". Optional; omitted from the PDF if None.

    Returns an in-memory BytesIO positioned at 0, ready for send_file().
    """
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        topMargin=22 * mm, bottomMargin=18 * mm,
        leftMargin=18 * mm, rightMargin=18 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "PerioTitle", parent=styles["Title"], fontSize=20, spaceAfter=2,
        textColor=colors.HexColor("#211f36"),
    )
    sub_style = ParagraphStyle(
        "PerioSub", parent=styles["Normal"], fontSize=10,
        textColor=colors.HexColor("#6b6885"), spaceAfter=14,
    )
    h2_style = ParagraphStyle(
        "PerioH2", parent=styles["Heading2"], fontSize=13, spaceBefore=14,
        spaceAfter=8, textColor=colors.HexColor("#4f46e5"),
    )
    body_style = ParagraphStyle(
        "PerioBody", parent=styles["Normal"], fontSize=10, leading=14,
    )
    legend_style = ParagraphStyle(
        "PerioLegend", parent=styles["Normal"], fontSize=8.5, leading=12,
        textColor=colors.HexColor("#211f36"),
    )

    stage = report.periodontitis_stage or "Not Classified"
    stage_color = _STAGE_COLORS.get(stage, colors.HexColor("#6b6885"))

    story = []


    # ---- Header (logo + title) --------------------------------------------
    header_text = [
        Paragraph("PerioDx Diagnostic Report", title_style),
        Paragraph(
            f"Report #{report.id} &nbsp;&middot;&nbsp; "
            f"Generated {report.report_date.strftime('%d %b %Y, %H:%M')}",
            sub_style
        ),
    ]

    if os.path.exists(LOGO_PATH):
        logo = Image(LOGO_PATH, width=46, height=46)
        header_table = Table(
            [[logo, header_text]],
            colWidths=[56, doc.width - 56],
        )
        header_table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (0, 0), 0),
            ("RIGHTPADDING", (0, 0), (0, 0), 10),
            ("LEFTPADDING", (1, 0), (1, 0), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(header_table)
    else:
        # Falls back to the old text-only header if the logo file is missing,
        # so a bad path never breaks report generation.
        story.extend(header_text)

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#e5e3f0"), thickness=1))
    story.append(Spacer(1, 10))

    # ---- Patient + report summary table ----------------------------------
    patient_name = f"{patient.first_name} {patient.last_name or ''}".strip()

    summary_data = [
        ["Patient", patient_name, "Patient ID", f"#{patient.id}"],
        ["Phone", patient.phone or "—", "Diabetic", "Yes" if patient.diabetic else "No"],
        ["Report ID", f"#{report.id}", "Periodontitis stage", stage],
    ]

    if generated_by is not None:
        clinician_name = f"{generated_by.first_name} {generated_by.last_name or ''}".strip()
        summary_data.append([
            "Generated by", clinician_name,
            "Clinician email", generated_by.email or "—"
        ])

    summary_table = Table(summary_data, colWidths=[70, 155, 100, 155])
    summary_style = [
        ("FONTSIZE", (0, 0), (-1, -1), 9.5),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#6b6885")),
        ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#6b6885")),
        ("TEXTCOLOR", (3, 2), (3, 2), stage_color),
        ("FONTNAME", (3, 2), (3, 2), "Helvetica-Bold"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LINEBELOW", (0, 0), (-1, -2), 0.5, colors.HexColor("#eeecf7")),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#fbfaff")),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e3f0")),
    ]
    summary_table.setStyle(TableStyle(summary_style))
    story.append(summary_table)

    if report.notes:
        story.append(Spacer(1, 10))
        story.append(Paragraph("<b>Notes</b>", body_style))
        story.append(Paragraph(report.notes.replace("\n", "<br/>"), body_style))

    # ---- Severity color legend --------------------------------------------
    # Mirrors the legend shown under the tooth map in records.html /
    # dashboard_user.html, so the color-coded rows in the per-tooth tables
    # below are self-explanatory without cross-referencing the web app.
    story.append(Spacer(1, 14))
    story.append(Paragraph("Bone Loss Severity Legend", h2_style))

    legend_items = [
        (_STATUS_ROW_COLORS["normal"], colors.HexColor("#00a67e"), "Normal (<15%)"),
        (_STATUS_ROW_COLORS["mild"], colors.HexColor("#c9880a"), "Mild (15–33%)"),
        (_STATUS_ROW_COLORS["moderate"], colors.HexColor("#c65a34"), "Moderate (33–50%)"),
        (_STATUS_ROW_COLORS["severe"], colors.HexColor("#b03030"), "Severe (>50%)"),
        (colors.HexColor("#f3f2f8"), colors.HexColor("#6b6885"), "Unmeasured / missing"),
    ]

    legend_row = []
    legend_style_cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]
    for idx, (fill, border, label) in enumerate(legend_items):
        swatch_table = Table([[""]], colWidths=[10], rowHeights=[10])
        swatch_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (0, 0), fill),
            ("BOX", (0, 0), (0, 0), 1, border),
        ]))
        legend_row.append(swatch_table)
        legend_row.append(Paragraph(label, legend_style))

    legend_col_widths = []
    for _ in legend_items:
        legend_col_widths += [14, 95]

    legend_table = Table([legend_row], colWidths=legend_col_widths)
    legend_table.setStyle(TableStyle(legend_style_cmds))
    story.append(legend_table)
    story.append(Spacer(1, 4))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#e5e3f0"), thickness=1))


    # ---- Visual tooth map (color-coded boxes, matches the web UI) --------
    jaw_label_style = ParagraphStyle(
        "PerioJawLabel", parent=styles["Normal"], fontSize=9,
        alignment=TA_CENTER, textColor=colors.HexColor("#6b6885"),
        fontName="Helvetica-Bold", spaceAfter=6,
    )

    story.append(Spacer(1, 8))
    story.append(Paragraph("Tooth Map", h2_style))

    story.append(Paragraph("UPPER JAW", jaw_label_style))
    story.append(ToothMapRow(teeth.get("upper", []), doc.width))
    story.append(Spacer(1, 16))

    story.append(Paragraph("LOWER JAW", jaw_label_style))
    story.append(ToothMapRow(teeth.get("lower", []), doc.width))
    story.append(Spacer(1, 14))

    story.append(HRFlowable(width="100%", color=colors.HexColor("#e5e3f0"), thickness=1))






    # ---- Per-tooth tables --------------------------------------------------
    def jaw_table(jaw_teeth, jaw_label):
        story.append(Paragraph(jaw_label, h2_style))
        header = ["FDI #", "Tooth", "Status", "Bone loss"]
        rows = [header]
        row_colors = []
        for t in jaw_teeth:
            status = t.get("status")
            name = FDI_TOOTH_NAMES.get(t["tooth_number"], t["tooth_number"])
            pct = t.get("bone_loss_pct")
            pct_display = f"{pct}%" if pct is not None else "—"
            rows.append([
                t["tooth_number"], name,
                _STATUS_LABELS.get(status, status or "—"),
                pct_display,
            ])
            row_colors.append(_STATUS_ROW_COLORS.get(status))

        tbl = Table(rows, colWidths=[45, 220, 100, 115], repeatRows=1)
        style = [
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#4f46e5")),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("LINEBELOW", (0, 0), (-1, -1), 0.4, colors.HexColor("#e5e3f0")),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#e5e3f0")),
        ]
        for i, c in enumerate(row_colors, start=1):
            if c:
                style.append(("BACKGROUND", (0, i), (-1, i), c))
        tbl.setStyle(TableStyle(style))
        story.append(tbl)
        story.append(Spacer(1, 12))

    jaw_table(teeth.get("upper", []), "Upper Jaw")
    jaw_table(teeth.get("lower", []), "Lower Jaw")

    story.append(Spacer(1, 6))
    story.append(HRFlowable(width="100%", color=colors.HexColor("#e5e3f0"), thickness=1))
    story.append(Paragraph(
        "Generated by PerioDx — AI-assisted periodontal screening. "
        "This report supports, but does not replace, clinical judgment.",
        sub_style
    ))

    doc.build(story)
    buffer.seek(0)
    return buffer