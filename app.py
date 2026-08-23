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
# Configure MySQL database connection (PythonAnywhere MySQL settings)
app.config['SQLALCHEMY_DATABASE_URI'] = F'mysql+pymysql://ruissmarthome:{PYMYSQL_KEY}@ruissmarthome.mysql.pythonanywhere-services.com/ruissmarthome$account'
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



# Define the User table
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


otp_store = {}





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
    # Create the database tables if they don't exist
    with app.app_context():
        db.create_all()

    app.run(debug=True)