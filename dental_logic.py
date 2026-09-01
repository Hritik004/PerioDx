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