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
from sqlalchemy.exc import OperationalError
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

# Sessions survive browser restarts instead of expiring the moment the
# browser session ends. session.permanent = True (set at login) is what
# makes Flask actually apply this lifetime to the cookie.
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

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
#
# NOTE ON CROSS-DATABASE OWNERSHIP:
# Report.created_by_user_id references User.id, but User lives on the
# default bind ("account" DB) and Report lives on the 'dental' bind
# ("dental_reports" DB) — two separate physical MySQL databases. MySQL
# (and SQLAlchemy) can't enforce a real FOREIGN KEY across databases, so
# created_by_user_id is a plain indexed BigInteger column. Ownership is
# enforced entirely in application code (see every route below that
# filters/checks against session['user_id']) — there is no DB-level
# guarantee, so any new route touching Report or Patient must remember
# to filter by created_by_user_id itself.
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
    "pool_recycle": 180
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

    # Which logged-in clinician (User.id, account DB) saved this report.
    # Can't be a real ForeignKey — users/reports live in two different
    # physical MySQL databases (see the DATABASES note above) — so this
    # is a plain indexed column, checked explicitly in every route that
    # reads or lists reports/patients.
    created_by_user_id = db.Column(
        db.BigInteger,
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

    # Create login session. session.permanent = True + the
    # PERMANENT_SESSION_LIFETIME set above means this cookie survives
    # browser restarts instead of expiring as soon as the browser closes.
    session.permanent = True
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

        # 4. Create Session (permanent so it survives browser restarts —
        # see PERMANENT_SESSION_LIFETIME above).
        session.permanent = True
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


# ==========================================
# --- NEW: CLEAR REPORT CACHE ---
# ==========================================
@app.route('/api/clear-report-cache', methods=['POST'])
def clear_report_cache():
    """
    Deletes every cached report JSON on disk that belongs to the logged-in
    user (the report_cache/<uuid>.json files written by /api/diagnose).

    This is scoped to the current user only — it never touches another
    clinician's cached reports — and it only affects the on-disk cache,
    not anything already persisted via /api/save-report (Report /
    ToothMeasurement rows are untouched). Once a file is removed here,
    /see-report and "See report" for that diagnosis can no longer render
    it, and /api/save-report for that report_id will start returning
    "Cached report not found."
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "message": "Not authenticated"}), 401

    user_id = session['user_id']
    cleared_count = 0

    for filename in os.listdir(REPORTS_DIR):
        if not filename.endswith('.json'):
            continue
        file_path = os.path.join(REPORTS_DIR, filename)
        try:
            with open(file_path, 'r') as f:
                record = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if record.get('user_id') != user_id:
            continue

        try:
            os.remove(file_path)
            cleared_count += 1
        except OSError:
            continue

    session.pop('last_report_id', None)

    return jsonify({"success": True, "cleared_count": cleared_count}), 200


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
        return redirect(url_for('login_student'))

    user = User.query.get(session['user_id'])
    if not user:
        session.clear()
        return redirect(url_for('login_student'))

    return render_template('records.html', user=user)



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





# SLM (small language model) chat server -- a SEPARATE Colab/ngrok tunnel
# from the diagnostic YOLO pipeline (COLAB_SERVER_URL above). Set this in
# .env once the periodx_slm notebook's last cell prints its static domain.
SLM_SERVER_URL = os.getenv(
    "SLM_SERVER_URL",
    "https://alive-chemicals-gusto.ngrok-free.dev"
)


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







@app.route('/api/slm-report-json/<report_id>', methods=['GET'])
def api_slm_report_json(report_id):
    """
    Returns the exact distilled per-tooth JSON that /ask_perio_ai sends to
    the SLM for this report_id. Backs the "Diagnostic report loaded"
    banner in periodx_chat.html -- lets a clinician inspect precisely
    what data the assistant is reasoning from, not the raw report_cache
    blob (which also carries the four base64 report images).
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    cached_report = load_cached_report(report_id, session['user_id'])
    if not cached_report:
        return jsonify({"success": False, "error": "Report not found."}), 404

    return jsonify({
        "success": True,
        "report_id": report_id,
        "report": build_tooth_report_json(cached_report)
    }), 200







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


@app.route('/ask_perio_ai', methods=['POST'])
def ask_perio_ai():
    """
    Proxies one chat turn to the PerioDx SLM (/api/chat on the notebook's
    ngrok tunnel), attaching the distilled per-tooth JSON -- not the raw
    report_cache blob -- so the model gets just the numbers it needs.
    """
    if 'user_id' not in session:
        return jsonify({"success": False, "error": "Not authenticated"}), 401

    data = request.get_json(silent=True) or {}
    user_prompt = (data.get('prompt') or '').strip()
    report_id = data.get('report_id')

    if not user_prompt:
        return jsonify({"success": False, "error": "No prompt provided"}), 400

    MAX_HISTORY_TURNS = 2  # last 6 messages (3 user+assistant pairs)
    history = data.get('history', [])
    if isinstance(history, list) and len(history) > MAX_HISTORY_TURNS:
        history = history[-MAX_HISTORY_TURNS:]

    payload = {
        "prompt": user_prompt,
        "history": history if isinstance(history, list) else []
    }

    report_meta = None
    if report_id:
        cached_report = load_cached_report(report_id, session['user_id'])
        if cached_report:
            payload["report"] = build_tooth_report_json(cached_report)
            report_meta = {
                "report_id": report_id,
                "periodontitis_stage": payload["report"]["periodontitis_stage_estimate"]
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
        return redirect(url_for('login_student'))
    return render_template('periodx_chat.html')












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