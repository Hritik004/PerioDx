from flask import Flask, session, request, jsonify, flash
from flask import render_template,  redirect, url_for, abort
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from werkzeug.security import generate_password_hash, check_password_hash
import random
import os
import uuid
import json
import requests
from datetime import datetime
import pymysql
from dotenv import load_dotenv
from sqlalchemy import text
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import math
from datetime import timedelta



BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')

load_dotenv(ENV_PATH)

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")
PYMYSQL_KEY = os.getenv("PYMYSQL_KEY")
EMAIL_ID = os.getenv("EMAIL_ID")
EMAIL_KEY = os.getenv("EMAIL_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# ---------------------------------------------------------------------------
# Colab / ngrok model server
#
# This is the base URL of the notebook running the 3-model YOLO pipeline
# (see the ngrok static domain printed when that notebook cell runs).
# Keep it in .env as COLAB_SERVER_URL so you don't have to edit code every
# time you restart the Colab runtime and get a new tunnel (if you ever drop
# the static domain).
# ---------------------------------------------------------------------------
COLAB_SERVER_URL = os.getenv(
    "COLAB_SERVER_URL",
    "https://sized-theodora-diatomaceous.ngrok-free.dev"
)

# Where generated reports (the 4 base64 images + per-tooth map) are cached
# on disk.
# IMPORTANT: these must NOT go into the Flask session — Flask's default
# session is a signed client-side cookie capped at ~4KB by browsers, and
# these images run into the megabytes. Stuffing them into the session
# blows past that limit, corrupts the Set-Cookie header, and truncates
# the HTTP response (that's the "session cookie is too large" / "OSError:
# write" error). Only the small report_id goes in the session.
REPORTS_DIR = os.path.join(BASE_DIR, 'report_cache')
os.makedirs(REPORTS_DIR, exist_ok=True)



app = Flask(__name__)






app.secret_key = APP_SECRET_KEY

# ---------------------------------------------------------------------------
# DATABASES
#
# IMPORTANT: Flask-SQLAlchemy 3.x registers its extension under a single
# app.extensions['sqlalchemy'] key. Creating two separate SQLAlchemy()
# instances and calling init_app(app) on both (as this file used to do,
# with `db` and `dental_db`) makes the SECOND init_app() call raise:
#   RuntimeError: A 'SQLAlchemy' instance has already been registered
#   on this Flask app.
#
# The fix: use ONE SQLAlchemy() instance for the whole app, and separate
# the two physical MySQL databases using SQLALCHEMY_BINDS + a per-model
# __bind_key__, instead of two SQLAlchemy objects.
#   - User lives on the default bind (SQLALCHEMY_DATABASE_URI), the
#     "account" database.
#   - Patient / Report / ToothMeasurement live on the 'dental' bind,
#     the "dental_reports" database.
# ---------------------------------------------------------------------------

# Default bind: account database
app.config['SQLALCHEMY_DATABASE_URI'] = (
    f'mysql+pymysql://ruissmarthome:{PYMYSQL_KEY}'
    '@ruissmarthome.mysql.pythonanywhere-services.com/'
    'ruissmarthome$account'
)

# Named bind: dental reports database
#
# NOTE: PythonAnywhere's shared MySQL only grants your user access to
# databases prefixed with "<username>$" — e.g. the account DB above is
# "ruissmarthome$account", not "account". The dental database needs the
# same prefix ("ruissmarthome$dental_reports"), or every query against it
# fails with:
#   (1044, "Access denied for user 'ruissmarthome'@'%' to database
#   'dental_reports'")
# Create it (or rename it) via the PythonAnywhere Databases tab as
# "dental_reports" — PythonAnywhere will store it as
# "ruissmarthome$dental_reports" automatically.
app.config['SQLALCHEMY_BINDS'] = {
    'dental': (
        f'mysql+pymysql://ruissmarthome:{PYMYSQL_KEY}'
        '@ruissmarthome.mysql.pythonanywhere-services.com/'
        'ruissmarthome$dental_reports'
    )
}

app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280
}

db = SQLAlchemy(app)




# Mail configuration
app.config['MAIL_SERVER'] = 'smtp.googlemail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USERNAME'] = EMAIL_ID
app.config['MAIL_PASSWORD'] = EMAIL_KEY  # <-- app password, not Gmail password
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USE_SSL'] = False

mail = Mail(app)


# Define the User table (default bind -> "account" database)
class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)

    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=True)

    email = db.Column(db.String(255), unique=True, nullable=False)

    password_hash = db.Column(db.String(255), nullable=True)

    auth_provider = db.Column(
        db.Enum("local", "google", name="auth_provider_enum"),
        nullable=False,
        default="local"
    )

    provider_user_id = db.Column(db.String(255), nullable=True)

    created_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        server_onupdate=db.func.current_timestamp(),
        nullable=False
    )




class Patient(db.Model):
    __bind_key__ = "dental"
    __tablename__ = "patients"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True
    )

    first_name = db.Column(
        db.String(100),
        nullable=False
    )

    last_name = db.Column(
        db.String(100),
        nullable=True
    )

    phone = db.Column(
        db.String(20),
        nullable=False,
        index=True
    )

    diabetic = db.Column(
        db.Boolean,
        nullable=False,
        default=False
    )

    created_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        nullable=False
    )

    updated_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        server_onupdate=db.func.current_timestamp(),
        nullable=False
    )

    reports = db.relationship(
        "Report",
        back_populates="patient",
        cascade="all, delete-orphan"
    )


class Report(db.Model):
    __bind_key__ = "dental"
    __tablename__ = "reports"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True
    )

    patient_id = db.Column(
        db.BigInteger,
        db.ForeignKey(
            "patients.id",
            ondelete="CASCADE",
            onupdate="CASCADE"
        ),
        nullable=False,
        index=True
    )

    report_date = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        nullable=False,
        index=True
    )

    periodontitis_stage = db.Column(
        db.Enum(
            "Stage I",
            "Stage II",
            "Stage III",
            "Stage IV",
            "Not Classified",
            name="periodontitis_stage_enum"
        ),
        nullable=False,
        default="Not Classified"
    )

    notes = db.Column(
        db.Text,
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        nullable=False
    )

    patient = db.relationship(
        "Patient",
        back_populates="reports"
    )

    tooth_measurements = db.relationship(
        "ToothMeasurement",
        back_populates="report",
        cascade="all, delete-orphan"
    )


class ToothMeasurement(db.Model):
    __bind_key__ = "dental"
    __tablename__ = "tooth_measurements"

    id = db.Column(
        db.BigInteger,
        primary_key=True,
        autoincrement=True
    )

    report_id = db.Column(
        db.BigInteger,
        db.ForeignKey(
            "reports.id",
            ondelete="CASCADE",
            onupdate="CASCADE"
        ),
        nullable=False,
        index=True
    )

    tooth_number = db.Column(
        db.Enum(
            "11", "12", "13", "14", "15", "16", "17", "18",
            "21", "22", "23", "24", "25", "26", "27", "28",
            "31", "32", "33", "34", "35", "36", "37", "38",
            "41", "42", "43", "44", "45", "46", "47", "48",
            name="tooth_number_enum"
        ),
        nullable=False
    )

    cej_ac_distance_px = db.Column(
        db.Numeric(10, 2),
        nullable=True
    )

    tooth_length_px = db.Column(
        db.Numeric(10, 2),
        nullable=True
    )

    created_at = db.Column(
        db.DateTime,
        server_default=db.func.current_timestamp(),
        nullable=False
    )

    report = db.relationship(
        "Report",
        back_populates="tooth_measurements"
    )

    __table_args__ = (
        db.UniqueConstraint(
            "report_id",
            "tooth_number",
            name="uq_report_tooth"
        ),
    )




otp_store = {}


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


def load_cached_report(report_id, owner_user_id):
    """Fetch a cached report JSON from disk and confirm the caller owns it."""
    if not report_id:
        return None
    report_path = os.path.join(REPORTS_DIR, f"{report_id}.json")
    if not os.path.exists(report_path):
        return None
    with open(report_path, 'r') as f:
        cached_report = json.load(f)
    if cached_report.get('user_id') != owner_user_id:
        abort(403)
    return cached_report





@app.route('/signup')
def signup_faculty():
    return render_template('signup.html',google_client_id=GOOGLE_CLIENT_ID)


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




@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('dashboard'))


@app.route('/login',methods=['GET'])
def login_student():
    return render_template('login.html',google_client_id=GOOGLE_CLIENT_ID)


@app.route('/login', methods=['POST'])
def login():

    data = request.get_json()

    email = data.get('email')
    password = data.get('password')


    # Validate input
    if not email or not password:
        return jsonify({
            "success": False,
            "message": "Email and password are required"
        }), 400


    # Find user by email
    user = User.query.filter_by(email=email).first()


    if not user:
        return jsonify({
            "success": False,
            "message": "User does not exist"
        }), 404


    # Check authentication provider
    if user.auth_provider != "local":
        return jsonify({
            "success": False,
            "message": "Please login using Google authentication"
        }), 400


    # Verify password
    if not check_password_hash(user.password_hash, password):
        return jsonify({
            "success": False,
            "message": "Invalid password"
        }), 401

    session.pop('_flashes', None)

    # Create login session
    session['user_id'] = user.id
    session['email'] = user.email
    session['name'] = user.first_name


    #flash("You have been successfully logged in.")

    return jsonify({
        "success": True,
        "message": "Login successful",
        "user": {
            "id": user.id,
            "name": user.first_name,
            "email": user.email
        }
    }), 200






# ==========================================
# --- GOOGLE LOGIN ROUTE ---
# ==========================================
@app.route('/google-login', methods=['POST'])
def google_login():
    data = request.get_json()
    token = data.get('id_token')

    if not token:
        return jsonify({"success": False, "message": "No token provided"}), 400

    try:
        # 1. Verify the Google Token
        idinfo = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )

        # 2. Extract user info
        google_id = idinfo['sub']
        email = idinfo['email']
        first_name = idinfo.get('given_name', 'Unknown')
        last_name = idinfo.get('family_name', '')

        # 3. Database Check
        user = User.query.filter_by(email=email).first()

        if not user:
            user = User(
                first_name=first_name,
                last_name=last_name,
                email=email,
                auth_provider='google',
                provider_user_id=google_id
            )
            db.session.add(user)
            db.session.commit()
        elif user.auth_provider == 'local':
            user.auth_provider = 'google'
            user.provider_user_id = google_id
            db.session.commit()
        else:
            user.first_name = first_name
            user.last_name = last_name
            db.session.commit()

        session.pop('_flashes', None)

        # 4. Create Session
        session['user_id'] = user.id
        session['email'] = user.email
        session['name'] = user.first_name

        return jsonify({
            "success": True,
            "message": "Logged in successfully with Google!"
        }), 200

    except ValueError:
        return jsonify({"success": False, "message": "Invalid Google token"}), 401

    except Exception as e:
        # THIS IS CRITICAL: If your DB schema is missing columns, it will print here.
        print(f"GOOGLE LOGIN FAILED: {str(e)}")
        db.session.rollback() # Prevent broken database state
        return jsonify({"success": False, "message": "Internal Server Error"}), 500





@app.route('/dashboard_user')
def dashboard_user():
    # Check whether the user is logged in
    if 'user_id' not in session:
        return redirect(url_for('login_student'))

    # Get the logged-in user's ID from the session
    user_id = session['user_id']

    # Fetch user from database
    user = User.query.get(user_id)

    # If user no longer exists, clear the session
    if not user:
        session.clear()
        return redirect(url_for('login_student'))

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
        # comment above). Only the small report_id is kept in the session
        # so /see-report can look it back up after a reload.
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
        return redirect(url_for('login_student'))

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
    stage from the per-tooth bone-loss map, then writes one Report row and
    one ToothMeasurement row per detected tooth.

    Expected JSON body:
      {
        "report_id": "<uuid from the cached report>",
        "patient_mode": "existing" | "new",

        # if patient_mode == "existing":
        "patient_id": 123,

        # if patient_mode == "new":
        "first_name": "...", "last_name": "...",
        "phone": "...", "diabetic": true/false,

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
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    report_id = data.get('report_id')
    patient_mode = data.get('patient_mode')

    cached_report = load_cached_report(report_id, session['user_id'])
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

        # ---- Compute stage + create the Report row ----------------------
        complexity_factors = data.get('complexity_factors') or {}
        if not isinstance(complexity_factors, dict):
            complexity_factors = {}
        stage = compute_periodontitis_stage(teeth, complexity_factors)

        report_row = Report(
            patient_id=patient.id,
            periodontitis_stage=stage,
            notes=(
                f"Auto-saved from diagnostic scan (cache id {report_id}). "
                "Stage estimated from radiographic bone loss % and missing-"
                "tooth count only — no clinical attachment loss (CAL) or "
                "probing data was available"
                + (
                    "; clinician-entered complexity factors were applied."
                    if any(complexity_factors.get(k) for k in COMPLEXITY_FACTOR_KEYS)
                    else "."
                )
            )
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

        return jsonify({
            "success": True,
            "message": "Report saved successfully.",
            "patient_id": patient.id,
            "report_id": report_row.id,
            "periodontitis_stage": stage,
            "teeth_saved": saved_count
        }), 200

    except Exception as e:
        db.session.rollback()
        print(f"SAVE REPORT FAILED: {str(e)}")
        return jsonify({"success": False, "message": "Failed to save report."}), 500


@app.route('/register', methods=['POST'])
def register():
    data = request.get_json()

    first_name = data.get('firstName')
    last_name = data.get('lastName')
    email = data.get('email')
    password = data.get('password')

    # Validate required fields
    if not first_name or not email or not password:
        return jsonify({'message': 'Missing required fields.'}), 400

    # Check if user already exists
    existing_user = User.query.filter_by(email=email).first()
    if existing_user:
        return jsonify({'message': 'User already exists. Please log in.'}), 400

    # Hash the password
    hashed_password = generate_password_hash(password)

    # Generate OTP
    otp = random.randint(100000, 999999)

    # Store registration data temporarily until OTP verification
    otp_store[email] = {
        'first_name': first_name,
        'last_name': last_name,
        'password_hash': hashed_password,
        'auth_provider': 'local',
        'provider_user_id': None,
        'otp': otp
    }

    # Send OTP email
    msg = Message(
        subject="OTP for ClassIQ",
        sender=("ClassIQ Support", app.config['MAIL_USERNAME']),
        recipients=[email]
    )

    msg.body = (
        f"{otp} is your OTP for ClassIQ account creation.\n\n"
        "Do not share this OTP with anyone."
    )

    try:
        mail.send(msg)
        print(f"OTP sent to {email}")
    except Exception as e:
        print(f"Error sending email: {e}")
        return jsonify({'message': 'Failed to send OTP. Please try again.'}), 500

    return jsonify({
        'message': 'OTP sent to your email.'
    }), 200





@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    data = request.json
    email = data.get('email')

    if email in otp_store:
        # Generate a new OTP
        otp = random.randint(100000, 999999)
        otp_store[email]['otp'] = otp

        # Simulate sending the new OTP (in production, use an email service)
        print(f"New OTP for {email}: {otp}")

        return jsonify({'message': 'A new OTP has been sent to your email address.'}), 200
    else:
        return jsonify({'message': 'Email not found. Please register again.'}), 400


@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    data = request.get_json()

    email = data.get('email')
    otp = int(data.get('otp'))

    # Check if OTP exists and matches
    if email in otp_store and otp_store[email]['otp'] == otp:

        # Get temporary user data
        user_data = otp_store[email]

        # Create user record
        new_user = User(
            first_name=user_data['first_name'],
            last_name=user_data['last_name'],
            email=email,
            password_hash=user_data['password_hash'],
            auth_provider='local',
            provider_user_id=None
        )

        db.session.add(new_user)
        db.session.commit()

        # Remove temporary OTP data
        del otp_store[email]

        return jsonify({
            'message': 'OTP verified successfully! Account created.'
        }), 200

    else:
        return jsonify({
            'message': 'Invalid OTP. Please try again.'
        }), 400





if __name__ == '__main__':
    # Create the database tables if they don't exist.
    # db.create_all() creates tables across ALL configured binds by default
    # (the default bind + every key in SQLALCHEMY_BINDS, i.e. 'dental'
    # here) — so a single call now covers both the account and dental
    # databases; there's no separate dental_db.create_all() anymore.
    with app.app_context():
        db.create_all()

    app.run(debug=True)