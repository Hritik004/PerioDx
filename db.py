"""
db.py

The database connection layer: one SQLAlchemy instance for the whole app,
plus every model. Bound to a real Flask app via `db.init_app(app)` in
app.py (after config.apply_to(app) has set SQLALCHEMY_DATABASE_URI /
SQLALCHEMY_BINDS) — this module itself has no dependency on app.py, so it
can be imported freely by auth.py, app.py, or anything else that needs a
model.
"""

from flask_sqlalchemy import SQLAlchemy

# ---------------------------------------------------------------------------
# IMPORTANT: Flask-SQLAlchemy 3.x registers its extension under a single
# app.extensions['sqlalchemy'] key. Creating two separate SQLAlchemy()
# instances and calling init_app(app) on both (as this project used to do,
# with `db` and `dental_db`) makes the SECOND init_app() call raise:
#   RuntimeError: A 'SQLAlchemy' instance has already been registered
#   on this Flask app.
#
# The fix: ONE SQLAlchemy() instance for the whole app, with the two
# physical MySQL databases separated by SQLALCHEMY_BINDS + a per-model
# __bind_key__ (see config.py for the actual URIs):
#   - User lives on the default bind, the "account" database.
#   - Patient / Report / ToothMeasurement live on the 'dental' bind, the
#     "dental_reports" database.
# ---------------------------------------------------------------------------
db = SQLAlchemy()


# Default bind -> "account" database
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
    # physical MySQL databases (see the module docstring above) — so this
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