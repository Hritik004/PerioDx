from flask import Flask, session, request, jsonify, flash
from flask import render_template, redirect, url_for, abort
import os
import uuid
import json
import requests
from datetime import datetime
from sqlalchemy.exc import OperationalError

import config
from db import db, User, Patient, Report, ToothMeasurement
from auth import auth_bp, mail

from dental_logic import (
    FDI_UPPER_ORDER,
    FDI_LOWER_ORDER,
    COMPLEXITY_FACTOR_KEYS,
    CUSTOM_NOTES_MAX_LEN,
    build_report_notes,
    compute_periodontitis_stage,
    compute_tooth_bone_loss,
    build_tooth_report_json,
    build_tooth_report_json_from_saved,
    load_cached_report,
    looks_degenerate,
)


REPORTS_DIR = config.REPORTS_DIR
os.makedirs(REPORTS_DIR, exist_ok=True)

COLAB_SERVER_URL = config.COLAB_SERVER_URL

# SLM (small language model) chat server -- a SEPARATE Colab/ngrok tunnel
# from the diagnostic YOLO pipeline (COLAB_SERVER_URL above).
SLM_SERVER_URL = config.SLM_SERVER_URL


app = Flask(__name__)

# Pulls in secret key, session lifetime, SQLALCHEMY_*, and MAIL_* settings
# — see config.py.
config.apply_to(app)

db.init_app(app)
mail.init_app(app)

# All login/signup/Google-OAuth/OTP routes -- see auth.py. Every
# url_for('login_student') in this file is now url_for('auth.login_student')
# because those routes live on this blueprint.
app.register_blueprint(auth_bp)


@app.route('/dashboard')
def dashboard():
    # If the user is already logged in, redirect them to their dashboard
    if 'user_id' in session:
        return redirect('/dashboard_user')

    # Otherwise, show the normal public/landing dashboard page
    return render_template('dashboard.html')


@app.route('/')
def root():
    return redirect(url_for('dashboard'))


@app.route('/dashboard_user')
def dashboard_user():
    # Check whether the user is logged in
    if 'user_id' not in session:
        return redirect(url_for('auth.login_student'))

    # Get the logged-in user's ID from the session
    user_id = session['user_id']

    # Fetch user from database
    user = User.query.get(user_id)

    # If user no longer exists, clear the session
    if not user:
        session.clear()
        return redirect(url_for('auth.login_student'))

    # Get scan statistics
    # Use 0 for now if you have not created a scans table yet
    scan_count = 0
    flagged_count = 0

    return render_template(
        'dashboard_user.html',
        user=user,
        scan_count=scan_count,
        flagged_count=flagged_count
    )


# ==========================================
# --- NEW: DIAGNOSTIC PIPELINE ROUTE ---
# ==========================================
@app.route('/api/diagnose', methods=['POST'])
def api_diagnose():
    """
    Receives a panoramic X-ray from the dashboard upload widget, forwards it
    to the Colab notebook's /predict endpoint (exposed via the static ngrok
    domain), and relays back the 4 generated images (model_a, model_b,
    model_c, combined) plus the per-tooth bone-loss map as base64 data URIs
    / JSON for the frontend to render.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    if 'image' not in request.files:
        return jsonify({"success": False, "message": "No image file provided"}), 400

    uploaded_file = request.files['image']
    if uploaded_file.filename == '':
        return jsonify({"success": False, "message": "Empty filename"}), 400

    try:
        files = {
            'image': (
                uploaded_file.filename,
                uploaded_file.stream,
                uploaded_file.mimetype or 'application/octet-stream'
            )
        }

        # ngrok free-tier domains show an interstitial warning page to any
        # request that doesn't send this header — without it, requests.post
        # would get back HTML instead of your JSON.
        headers = {"ngrok-skip-browser-warning": "true"}

        colab_response = requests.post(
            f"{COLAB_SERVER_URL}/predict",
            files=files,
            headers=headers,
            timeout=90
        )
        colab_response.raise_for_status()
        result = colab_response.json()

        if not result.get('success'):
            return jsonify({
                "success": False,
                "message": result.get('error', 'Diagnostic server returned an error')
            }), 502

        images = result.get('images', {})
        # The model server returns this as {"upper": [...16], "lower": [...16]}
        # (see build_teeth_map() in colab_server.py). May be absent on older
        # server versions, so default to None and let the frontend show its
        # "per-tooth data not available" state.
        teeth = result.get('teeth')
        report_id = str(uuid.uuid4())

        # Cache the report on disk (NOT in the session — see REPORTS_DIR
        # comment in config.py). Only the small report_id is kept in the
        # session so /see-report can look it back up after a reload.
        #
        # This cache entry is temporary: once the clinician saves this
        # report to a patient record (see /api/save-report below), the
        # cache file for this report_id is deleted automatically and
        # everything downstream (report images, the AI assistant, the
        # JSON viewer) switches over to reading the persisted DB copy
        # instead.
        report_record = {
            "id": report_id,
            "user_id": session['user_id'],
            "created_at": datetime.utcnow().isoformat(),
            "images": images,
            "teeth": teeth
        }
        report_path = os.path.join(REPORTS_DIR, f"{report_id}.json")
        with open(report_path, 'w') as f:
            json.dump(report_record, f)

        session['last_report_id'] = report_id

        return jsonify({
            "success": True,
            "report_id": report_id,
            "images": images,
            "teeth": teeth
        }), 200

    except requests.exceptions.Timeout:
        return jsonify({
            "success": False,
            "message": "The diagnostic server took too long to respond. Please try again."
        }), 504
    except requests.exceptions.ConnectionError:
        return jsonify({
            "success": False,
            "message": "Could not reach the diagnostic server. Make sure the Colab notebook and ngrok tunnel are running."
        }), 502
    except requests.exceptions.RequestException as e:
        return jsonify({
            "success": False,
            "message": f"Diagnostic server error: {str(e)}"
        }), 502
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route('/see-report')
def see_report():
    """Standalone page to revisit the most recent report (e.g. after a reload)."""
    if 'user_id' not in session:
        return redirect(url_for('auth.login_student'))

    report_id = session.get('last_report_id')
    report_path = os.path.join(REPORTS_DIR, f"{report_id}.json") if report_id else None

    if not report_id or not os.path.exists(report_path):
        flash("No report available yet. Run a diagnosis first.")
        return redirect(url_for('dashboard_user'))

    with open(report_path, 'r') as f:
        report = json.load(f)

    # Make sure users can't view each other's reports by guessing/reusing an id.
    if report.get('user_id') != session['user_id']:
        abort(403)

    return render_template('see_report.html', report=report)


# ==========================================
# --- NEW: PATIENT LOOKUP (for "existing patient" save flow) ---
# ==========================================
@app.route('/api/patients/<int:patient_id>', methods=['GET'])
def get_patient(patient_id):
    # Intentionally NOT scoped to created_by_user_id: this endpoint only
    # backs the "attach this diagnosis to an existing patient" step of the
    # save flow, where a clinician needs to look up a patient (possibly
    # entered by a colleague) purely by ID to confirm it exists before
    # saving a new report against it. Report/patient VISIBILITY is what's
    # scoped elsewhere (api_search_patients, api_patient_reports,
    # api_report_detail) — this lookup deliberately stays open so patient
    # records can be shared across clinicians for the save step. If you
    # want fully siloed patients (no cross-clinician reuse), this route
    # needs the same created_by_user_id check as the others.
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    patient = Patient.query.get(patient_id)
    if not patient:
        return jsonify({"success": False, "message": f"No patient found with ID {patient_id}."}), 404

    return jsonify({
        "success": True,
        "patient": {
            "id": patient.id,
            "first_name": patient.first_name,
            "last_name": patient.last_name,
            "phone": patient.phone,
            "diabetic": patient.diabetic
        }
    }), 200


# ==========================================
# --- NEW: SAVE REPORT (creates Report + ToothMeasurement rows) ---
# ==========================================
@app.route('/api/save-report', methods=['POST'])
def save_report():
    """
    Persists a cached (in-memory JSON) diagnostic report into the dental
    database: resolves or creates the Patient, calculates a periodontitis
    stage from the per-tooth bone-loss map, then writes one Report row
    (tagged with the saving clinician's user id) and one ToothMeasurement
    row per detected tooth.

    Expected JSON body:
      {
        "report_id": "<uuid from the cached report>",
        "patient_mode": "existing" | "new",

        # if patient_mode == "existing":
        "patient_id": 123,

        # if patient_mode == "new":
        "first_name": "...", "last_name": "...",
        "phone": "...", "diabetic": true/false,

        # optional — free text the clinician typed at save time. Combined
        # with the complexity factors below into Report.notes via
        # build_report_notes(). Trimmed and capped at
        # CUSTOM_NOTES_MAX_LEN characters.
        "notes": "...",

        # optional — clinician-entered complexity factors that can escalate
        # Stage III to Stage IV (see COMPLEXITY_FACTOR_KEYS). Any subset of:
        "complexity_factors": {
          "probing_depth_6mm_plus": true/false,
          "vertical_bone_defect_3mm_plus": true/false,
          "furcation_class_2_3": true/false,
          "fewer_than_20_teeth": true/false,
          "bite_collapse": true/false
        }
      }

    RETRY NOTE: PythonAnywhere's shared MySQL occasionally drops an idle
    pooled connection between pool_pre_ping's ping and the actual query
    (pymysql.err.OperationalError 2013, "Lost connection to MySQL server
    during query"). That's a transient infrastructure hiccup, not bad
    data — nothing has been flushed/committed yet when the very first
    INSERT hits it, so it's safe to roll back and retry once on a fresh
    connection rather than surfacing a hard failure to the clinician.

    CACHE NOTE: once the report is durably committed to the database, the
    on-disk cache file for report_id is deleted automatically (see the
    end of the successful branch below) — there's no more need to keep it
    around, and everything downstream (report images, the AI assistant's
    per-tooth JSON, the JSON viewer) should read from the saved DB copy
    (report_row.id / "saved_report_id") from that point on, not the old
    cache report_id.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    report_id = data.get('report_id')
    patient_mode = data.get('patient_mode')

    cached_report = load_cached_report(report_id, session['user_id'], REPORTS_DIR)
    if not cached_report:
        return jsonify({
            "success": False,
            "message": "Cached report not found. Please re-run the diagnosis."
        }), 404

    teeth = cached_report.get('teeth')
    if not teeth or not teeth.get('upper') or not teeth.get('lower'):
        return jsonify({
            "success": False,
            "message": "This report has no per-tooth data, so it can't be saved."
        }), 400

    if patient_mode not in ('existing', 'new'):
        return jsonify({"success": False, "message": "patient_mode must be 'existing' or 'new'."}), 400

    max_attempts = 2
    for attempt in range(1, max_attempts + 1):
        try:
            # ---- Resolve or create the patient -----------------------------
            if patient_mode == 'existing':
                raw_patient_id = data.get('patient_id')
                if not raw_patient_id:
                    return jsonify({"success": False, "message": "Patient ID is required."}), 400
                try:
                    patient_id = int(raw_patient_id)
                except (TypeError, ValueError):
                    return jsonify({"success": False, "message": "Patient ID must be a number."}), 400

                patient = Patient.query.get(patient_id)
                if not patient:
                    return jsonify({
                        "success": False,
                        "message": f"No patient found with ID {patient_id}."
                    }), 404

            else:  # patient_mode == 'new'
                first_name = (data.get('first_name') or '').strip()
                last_name = (data.get('last_name') or '').strip()
                phone = (data.get('phone') or '').strip()
                diabetic = bool(data.get('diabetic', False))

                if not first_name or not phone:
                    return jsonify({
                        "success": False,
                        "message": "First name and phone are required for a new patient."
                    }), 400

                patient = Patient(
                    first_name=first_name,
                    last_name=last_name or None,
                    phone=phone,
                    diabetic=diabetic
                )
                db.session.add(patient)
                db.session.flush()  # assigns patient.id without committing yet

            # ---- Compute stage + build notes from what was actually
            #      selected/typed at save time -------------------------------
            complexity_factors = data.get('complexity_factors') or {}
            if not isinstance(complexity_factors, dict):
                complexity_factors = {}
            stage = compute_periodontitis_stage(teeth, complexity_factors)

            custom_notes = (data.get('notes') or '').strip()
            if len(custom_notes) > CUSTOM_NOTES_MAX_LEN:
                custom_notes = custom_notes[:CUSTOM_NOTES_MAX_LEN]

            report_row = Report(
                patient_id=patient.id,
                created_by_user_id=session['user_id'],
                periodontitis_stage=stage,
                notes=build_report_notes(complexity_factors, custom_notes)
            )
            db.session.add(report_row)
            db.session.flush()  # assigns report_row.id

            # ---- Write one ToothMeasurement row per detected tooth ----------
            saved_count = 0
            for jaw_key, order in (('upper', FDI_UPPER_ORDER), ('lower', FDI_LOWER_ORDER)):
                jaw_teeth = teeth.get(jaw_key) or []
                for tooth_data, tooth_number in zip(jaw_teeth, order):
                    status = tooth_data.get('status')
                    # Skip positions where no tooth was detected at all.
                    if status in (None, 'missing'):
                        continue

                    measurement = ToothMeasurement(
                        report_id=report_row.id,
                        tooth_number=tooth_number,
                        # Raw pixel distances from the model server (colab_server.py):
                        # cej_ac_distance_px = CEJ->alveolar-crest distance,
                        # tooth_length_px = CEJ->apex distance. Both are populated
                        # whenever the CEJ/AC segmentation masks intersected the
                        # tooth's long axis; otherwise NULL (status "unmeasured").
                        cej_ac_distance_px=tooth_data.get('cej_ac_distance_px'),
                        tooth_length_px=tooth_data.get('tooth_length_px')
                    )
                    db.session.add(measurement)
                    saved_count += 1

            db.session.commit()

            # ---- Auto-clear the on-disk cache entry for this report --------
            # The report now lives durably in the database, so the temporary
            # cache file is no longer needed. Removing it here (rather than
            # requiring a manual "clear cache" action) keeps the cache from
            # accumulating stale/duplicate copies of reports that are
            # already saved, and it's what pushes /ask_perio_ai and the
            # JSON viewer over to the saved-report code path (report_id
            # will no longer resolve via load_cached_report, so callers
            # must use the new saved_report_id going forward).
            try:
                if report_id:
                    stale_cache_path = os.path.join(REPORTS_DIR, f"{report_id}.json")
                    if os.path.exists(stale_cache_path):
                        os.remove(stale_cache_path)
            except OSError as cache_err:
                # Never fail the save because cache cleanup failed — the
                # report is already safely committed at this point.
                print(f"SAVE REPORT: could not clear cache for {report_id}: {cache_err}")

            if session.get('last_report_id') == report_id:
                session.pop('last_report_id', None)

            return jsonify({
                "success": True,
                "message": "Report saved successfully.",
                "patient_id": patient.id,
                "report_id": report_row.id,
                "periodontitis_stage": stage,
                "teeth_saved": saved_count
            }), 200

        except OperationalError as e:
            # Transient dropped-connection error (PythonAnywhere shared MySQL).
            # pool_pre_ping usually catches this, but there's a race where the
            # ping succeeds and the connection dies right after — rollback and
            # retry once on a fresh connection before giving up.
            db.session.rollback()
            if attempt < max_attempts:
                print(f"SAVE REPORT: transient DB error on attempt {attempt}, retrying: {e}")
                continue
            print(f"SAVE REPORT FAILED after {max_attempts} attempts: {str(e)}")
            return jsonify({
                "success": False,
                "message": "Database connection was interrupted. Please try saving again."
            }), 500

        except Exception as e:
            db.session.rollback()
            print(f"SAVE REPORT FAILED: {str(e)}")
            return jsonify({"success": False, "message": "Failed to save report."}), 500


@app.route('/records')
def records():
    """Patients -> their saved reports -> per-tooth bone-loss detail."""
    if 'user_id' not in session:
        return redirect(url_for('auth.login_student'))

    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('auth.login_student'))

    return render_template('records.html', user=user)


@app.route('/api/patients', methods=['GET'])
def api_search_patients():
    """
    Lists/searches patients THIS USER has at least one saved report for.
    A patient is only visible here if the logged-in clinician has
    created_by_user_id == session['user_id'] on at least one Report tied
    to that patient — patients another clinician has reported on, with no
    report from this user, never appear.

    Optional ?q= matches, in a single pass:
      - patient first or last name, partial match
      - exact patient ID (if q is numeric)
    Phone number and report-number lookup are intentionally NOT matched —
    search is name/ID only.
    With no q, returns the 100 most recently created matching patients.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    user_id = session['user_id']
    q = (request.args.get('q') or '').strip()

    # Base scope: only patients with >=1 report belonging to this user.
    query = (
        Patient.query
        .join(Report, Report.patient_id == Patient.id)
        .filter(Report.created_by_user_id == user_id)
        .distinct()
    )

    if q:
        # Name (first or last, partial) or exact patient ID only.
        filters = [
            Patient.first_name.ilike(f'%{q}%'),
            Patient.last_name.ilike(f'%{q}%'),
        ]
        if q.isdigit():
            filters.append(Patient.id == int(q))
        query = query.filter(db.or_(*filters))

    patients = query.order_by(Patient.id.desc()).limit(100).all()

    results = []
    for p in patients:
        # report_count / last_report_date must reflect only THIS USER's
        # reports on the patient, not the patient's full history across
        # every clinician — so filter p.reports in Python rather than
        # trusting the relationship as-is.
        own_reports = [r for r in p.reports if r.created_by_user_id == user_id]
        last_report_date = max((r.report_date for r in own_reports), default=None)
        results.append({
            "id": p.id,
            "first_name": p.first_name,
            "last_name": p.last_name or "",
            "phone": p.phone,
            "diabetic": p.diabetic,
            "report_count": len(own_reports),
            "last_report_date": last_report_date.isoformat() if last_report_date else None
        })

    return jsonify({"success": True, "patients": results}), 200


@app.route('/api/patients/<int:patient_id>/reports', methods=['GET'])
def api_patient_reports(patient_id):
    """All of THIS USER'S saved reports for one patient, newest first.

    If the logged-in clinician has never saved a report for this patient
    (even if the patient exists via another clinician's work), this
    returns 404 — the patient's PII (name, phone, diabetic status) is not
    exposed, and no other clinician's reports are listed.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    user_id = session['user_id']

    patient = Patient.query.get(patient_id)
    if not patient:
        return jsonify({"success": False, "message": f"No patient found with ID {patient_id}."}), 404

    reports = (
        Report.query
        .filter_by(patient_id=patient_id, created_by_user_id=user_id)
        .order_by(Report.report_date.desc())
        .all()
    )

    if not reports:
        # This user has no reports on this patient — treat the patient as
        # not found for them rather than leaking that the patient exists
        # under another clinician's account.
        return jsonify({"success": False, "message": f"No patient found with ID {patient_id}."}), 404

    return jsonify({
        "success": True,
        "patient": {
            "id": patient.id,
            "first_name": patient.first_name,
            "last_name": patient.last_name or "",
            "phone": patient.phone,
            "diabetic": patient.diabetic
        },
        "reports": [
            {
                "id": r.id,
                "report_date": r.report_date.isoformat(),
                "periodontitis_stage": r.periodontitis_stage,
                "notes": r.notes,
                "tooth_count": len(r.tooth_measurements)
            }
            for r in reports
        ]
    }), 200


@app.route('/api/reports/<int:report_id>', methods=['GET'])
def api_report_detail(report_id):
    """
    One saved report's full detail: patient info + a 32-position FDI
    tooth map, with bone-loss % and severity computed live (not stored)
    from each tooth's cej_ac_distance_px / tooth_length_px.

    Only the clinician who originally saved this report (created_by_user_id
    == session['user_id']) can view it. A report belonging to someone else
    is reported as "not found" rather than 403, so its existence isn't
    leaked to users who don't own it.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    report = Report.query.get(report_id)
    if not report or report.created_by_user_id != session['user_id']:
        return jsonify({"success": False, "message": f"No report found with ID {report_id}."}), 404

    patient = report.patient
    measurements_by_tooth = {m.tooth_number: m for m in report.tooth_measurements}

    def build_jaw(order):
        jaw = []
        for tooth_number in order:
            m = measurements_by_tooth.get(tooth_number)
            if m is None:
                # No ToothMeasurement row for this position -> the model
                # never detected a tooth here when the report was saved.
                jaw.append({
                    "tooth_number": tooth_number,
                    "status": "missing",
                    "bone_loss_pct": None,
                    "cej_ac_distance_px": None,
                    "tooth_length_px": None
                })
                continue

            pct, status = compute_tooth_bone_loss(m.cej_ac_distance_px, m.tooth_length_px)
            jaw.append({
                "tooth_number": tooth_number,
                "status": status,
                "bone_loss_pct": pct,
                "cej_ac_distance_px": float(m.cej_ac_distance_px) if m.cej_ac_distance_px is not None else None,
                "tooth_length_px": float(m.tooth_length_px) if m.tooth_length_px is not None else None
            })
        return jaw

    teeth = {
        "upper": build_jaw(FDI_UPPER_ORDER),
        "lower": build_jaw(FDI_LOWER_ORDER)
    }

    return jsonify({
        "success": True,
        "report": {
            "id": report.id,
            "report_date": report.report_date.isoformat(),
            "periodontitis_stage": report.periodontitis_stage,
            "notes": report.notes
        },
        "patient": {
            "id": patient.id,
            "first_name": patient.first_name,
            "last_name": patient.last_name or "",
            "phone": patient.phone,
            "diabetic": patient.diabetic
        },
        "teeth": teeth
    }), 200


@app.route('/api/slm-saved-report-json/<int:report_id>', methods=['GET'])
def api_slm_saved_report_json(report_id):
    """Distilled per-tooth JSON for a *saved* report — backs the JSON-view
    banner in periodx_chat.html when opened from records.html."""
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    report = Report.query.get(report_id)
    if not report or report.created_by_user_id != session['user_id']:
        return jsonify({"success": False, "error": "Report not found."}), 404

    return jsonify({
        "success": True,
        "saved_report_id": report_id,
        "report": build_tooth_report_json_from_saved(report)
    }), 200


@app.route('/api/slm-report-json/<report_id>', methods=['GET'])
def api_slm_report_json(report_id):
    """
    Returns the exact distilled per-tooth JSON that /ask_perio_ai sends to
    the SLM for this report_id. Backs the "Diagnostic report loaded"
    banner in periodx_chat.html -- lets a clinician inspect precisely
    what data the assistant is reasoning from, not the raw report_cache
    blob (which also carries the four base64 report images).

    NOTE: once a report has been saved, its cache entry no longer exists
    (see the auto-clear step in /api/save-report) — callers should switch
    to /api/slm-saved-report-json/<saved_report_id> at that point instead
    of continuing to call this endpoint with the old cache report_id.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    cached_report = load_cached_report(report_id, session['user_id'], REPORTS_DIR)
    if not cached_report:
        return jsonify({"success": False, "error": "Report not found."}), 404

    return jsonify({
        "success": True,
        "report_id": report_id,
        "report": build_tooth_report_json(cached_report)
    }), 200


@app.route('/ask_perio_ai', methods=['POST'])
def ask_perio_ai():
    """
    Proxies one chat turn to the PerioDx SLM (/api/chat on the notebook's
    ngrok tunnel), attaching the distilled per-tooth JSON -- not the raw
    report_cache blob -- so the model gets just the numbers it needs.

    Once a report has been saved (see /api/save-report), its cache entry
    is deleted automatically, so the frontend passes saved_report_id
    instead of report_id from that point on. saved_report_id is checked
    first below and, when present, is always sourced live from the
    database rather than the (now-gone) cache file.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}

    user_prompt = (data.get('prompt') or '').strip()
    report_id = data.get('report_id')
    saved_report_id = data.get('saved_report_id')

    if not user_prompt:
        return jsonify({"success": False, "error": "No prompt provided"}), 400

    MAX_HISTORY_TURNS = 4  # last 6 messages (3 user+assistant pairs)
    history = data.get('history', [])
    if isinstance(history, list) and len(history) > MAX_HISTORY_TURNS:
        history = history[-MAX_HISTORY_TURNS:]

    payload = {
        "prompt": user_prompt,
        "history": history if isinstance(history, list) else []
    }

    report_meta = None

    # Saved database report takes priority
    if saved_report_id:
        try:
            saved_id_int = int(saved_report_id)
        except (TypeError, ValueError):
            saved_id_int = None

        if saved_id_int:
            report = Report.query.get(saved_id_int)

            if report and report.created_by_user_id == session['user_id']:
                payload["report"] = build_tooth_report_json_from_saved(report)

                report_meta = {
                    "saved_report_id": saved_id_int,
                    "periodontitis_stage": report.periodontitis_stage
                }

    # Otherwise use temporary cached report
    elif report_id:
        cached_report = load_cached_report(
            report_id,
            session['user_id'],
            REPORTS_DIR
        )

        if cached_report:
            payload["report"] = build_tooth_report_json(cached_report)

            report_meta = {
                "report_id": report_id,
                "periodontitis_stage": (
                    payload["report"]["periodontitis_stage_estimate"]
                )
            }

    headers = {"ngrok-skip-browser-warning": "true"}

    try:
        slm_response = requests.post(
            f"{SLM_SERVER_URL}/api/chat",
            json=payload,
            headers=headers,
            timeout=120
        )
        slm_response.raise_for_status()
        result = slm_response.json()
    except requests.exceptions.Timeout:
        return jsonify({"success": False, "error": "The AI assistant took too long to respond."}), 504
    except requests.exceptions.ConnectionError:
        return jsonify({"success": False, "error": "Could not reach the AI assistant server."}), 502
    except requests.exceptions.RequestException as e:
        return jsonify({"success": False, "error": f"AI assistant error: {str(e)}"}), 502
    except ValueError:
        return jsonify({"success": False, "error": "AI assistant returned an invalid response."}), 502

    if not result.get('success'):
        return jsonify({"success": False, "error": result.get('error', 'AI assistant returned an error')}), 502

    response_text = result.get('response', '')
    if looks_degenerate(response_text):
        response_text = (
            "Sorry, I ran into trouble generating a clear answer to that. "
            "Could you ask about one thing at a time?"
        )

    response_payload = {"success": True, "response": response_text}
    if report_meta:
        response_payload["report"] = report_meta

    return jsonify(response_payload), 200


@app.route('/clear_chat', methods=['POST'])
def clear_chat():
    # The SLM is stateless per request -- periodx_chat.html sends its full
    # conversationHistory with every /ask_perio_ai call, so there's no
    # server-side memory to clear yet. Kept as a no-op so the frontend's
    # DOMContentLoaded call to /clear_chat doesn't 404.
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401
    return jsonify({"success": True}), 200


@app.route('/chat')
def chat_page():
    if 'user_id' not in session:
        return redirect(url_for('auth.login_student'))
    return render_template('periodx_chat.html')


if __name__ == '__main__':
    # Create the database tables if they don't exist.
    # db.create_all() creates tables across ALL configured binds by default
    # (the default bind + every key in SQLALCHEMY_BINDS, i.e. 'dental'
    # here) — so a single call now covers both the account and dental
    # databases; there's no separate dental_db.create_all() anymore.
    with app.app_context():
        db.create_all()

    app.run(debug=True)