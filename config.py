"""
config.py

All environment loading and Flask/SQLAlchemy/Mail configuration values in
one place. Nothing here touches a live Flask `app` object except the
`apply_to()` helper, which copies these settings onto one — so this module
can be imported by db.py, auth.py, and app.py without any circular
dependency on any of them.
"""

import os
from datetime import timedelta
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, '.env')
load_dotenv(ENV_PATH)

APP_SECRET_KEY = os.getenv("APP_SECRET_KEY")
PYMYSQL_KEY = os.getenv("PYMYSQL_KEY")
EMAIL_ID = os.getenv("EMAIL_ID")
EMAIL_KEY = os.getenv("EMAIL_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")

# ---------------------------------------------------------------------------
# Colab / ngrok model servers
#
# COLAB_SERVER_URL: the 3-model YOLO diagnostic pipeline (see the ngrok
# static domain printed when that notebook cell runs).
# SLM_SERVER_URL: the SEPARATE small-language-model chat server tunnel
# (periodx_slm notebook). Keep both in .env so you don't have to edit code
# every time you restart a Colab runtime and get a new tunnel.
# ---------------------------------------------------------------------------
COLAB_SERVER_URL = os.getenv(
    "COLAB_SERVER_URL",
    "https://mutation-estimate-swarm.ngrok-free.dev"
)
SLM_SERVER_URL = os.getenv(
    "SLM_SERVER_URL",
    "https://alive-chemicals-gusto.ngrok-free.dev"
)

# Where generated reports (the 4 base64 images + per-tooth map) are cached
# on disk.
# IMPORTANT: these must NOT go into the Flask session — Flask's default
# session is a signed client-side cookie capped at ~4KB by browsers, and
# these images run into the megabytes. Stuffing them into the session
# blows past that limit, corrupts the Set-Cookie header, and truncates the
# HTTP response. Only the small report_id goes in the session.
REPORTS_DIR = os.path.join(BASE_DIR, 'report_cache')

# ---------------------------------------------------------------------------
# DATABASES
#
# Two separate physical MySQL databases, wired into ONE SQLAlchemy
# instance (see db.py) via SQLALCHEMY_BINDS + a per-model __bind_key__:
#   - default bind (SQLALCHEMY_DATABASE_URI): "account" DB -> User
#   - 'dental' bind: "dental_reports" DB -> Patient, Report,
#     ToothMeasurement
#
# NOTE: PythonAnywhere's shared MySQL only grants your user access to
# databases prefixed with "<username>$" — e.g. "ruissmarthome$account",
# not "account". Create/rename the dental DB as "dental_reports" via the
# PythonAnywhere Databases tab; it'll be stored as
# "ruissmarthome$dental_reports" automatically.
# ---------------------------------------------------------------------------
SQLALCHEMY_DATABASE_URI = (
    f'mysql+pymysql://ruissmarthome:{PYMYSQL_KEY}'
    '@ruissmarthome.mysql.pythonanywhere-services.com/'
    'ruissmarthome$account'
)
SQLALCHEMY_BINDS = {
    'dental': (
        f'mysql+pymysql://ruissmarthome:{PYMYSQL_KEY}'
        '@ruissmarthome.mysql.pythonanywhere-services.com/'
        'ruissmarthome$dental_reports'
    )
}
SQLALCHEMY_TRACK_MODIFICATIONS = False
SQLALCHEMY_ENGINE_OPTIONS = {
    "pool_pre_ping": True,
    "pool_recycle": 180
}

# Sessions survive browser restarts instead of expiring the moment the
# browser session ends. session.permanent = True (set at login, in
# auth.py) is what makes Flask actually apply this lifetime to the cookie.
PERMANENT_SESSION_LIFETIME = timedelta(days=7)

# Mail configuration (OTP emails, sent from auth.py)
MAIL_SERVER = 'smtp.googlemail.com'
MAIL_PORT = 587
MAIL_USERNAME = EMAIL_ID
MAIL_PASSWORD = EMAIL_KEY  # <-- app password, not Gmail password
MAIL_USE_TLS = True
MAIL_USE_SSL = False


def apply_to(app):
    """Copy this module's Flask/SQLAlchemy/Mail settings onto `app.config`."""
    app.secret_key = APP_SECRET_KEY
    for key in (
        'PERMANENT_SESSION_LIFETIME',
        'SQLALCHEMY_DATABASE_URI',
        'SQLALCHEMY_BINDS',
        'SQLALCHEMY_TRACK_MODIFICATIONS',
        'SQLALCHEMY_ENGINE_OPTIONS',
        'MAIL_SERVER',
        'MAIL_PORT',
        'MAIL_USERNAME',
        'MAIL_PASSWORD',
        'MAIL_USE_TLS',
        'MAIL_USE_SSL',
    ):
        app.config[key] = globals()[key]