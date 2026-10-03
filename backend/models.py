"""
Production database models for the Form 16 application.

- Local development continues to work with SQLite.
- Production can use DATABASE_URL (PostgreSQL recommended).
- Existing model contracts remain compatible with the current main.py.
- New integrations use companion tables so the current deployed SQLite schema
  remains usable until Phase 2 migration runs.
- Importing this module does not mutate the database schema.
"""

import os
from datetime import datetime

from sqlalchemy import (
    create_engine,
    Column,
    String,
    Float,
    Integer,
    Boolean,
    DateTime,
    ForeignKey,
    JSON,
    Text,
    LargeBinary,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship


DEFAULT_SQLITE_URL = "sqlite:///./form16_database.db"


def _normalise_database_url(raw_url):
    """Return a SQLAlchemy-compatible database URL."""

    url = (raw_url or DEFAULT_SQLITE_URL).strip()
    if not url:
        url = DEFAULT_SQLITE_URL

    # Some providers still expose the historical postgres:// scheme.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    return url


SQLALCHEMY_DATABASE_URL = _normalise_database_url(
    os.getenv("DATABASE_URL")
)


def _build_engine():
    options = {"pool_pre_ping": True}

    if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
    else:
        options["pool_recycle"] = 1800

    return create_engine(SQLALCHEMY_DATABASE_URL, **options)


engine = _build_engine()

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)

Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)

    # Contact details are stored here permanently.
    # The user-flow pages/main.py will be updated later to actually persist them.
    email = Column(String, unique=True, index=True, nullable=True)
    mobile = Column(String, unique=True, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    ledgers = relationship(
        "MonthlyLedger",
        back_populates="user",
        cascade="all, delete-orphan",
    )

    employee_details = relationship(
        "EmployeeDetail",
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
    )

    payments = relationship(
        "Payment",
        back_populates="user",
    )

    visitor_sessions = relationship(
        "VisitorSession",
        back_populates="user",
    )

    form16_generations = relationship(
        "Form16Generation",
        back_populates="user",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="user",
    )


class AdminSettings(Base):
    __tablename__ = "admin_settings"

    id = Column(Integer, primary_key=True)

    fee_amount = Column(Float, default=150.0)
    upi_id = Column(String)

    telegram_bot_token = Column(String)
    telegram_chat_id = Column(String)

    # Admin-selected March -> February payroll/Form-16 cycle.
    # Example: "2026-27"
    financial_year = Column(String)

    # Tax configuration can later be loaded dynamically.
    tax_slabs_json = Column(JSON, nullable=True)


class EmployerCache(Base):
    """
    TAN-based employer/DDO cache.
    Once saved, the same TAN can auto-fill employer details for later users.
    """

    __tablename__ = "employers_by_tan"

    tan = Column(String, primary_key=True, index=True)

    officer_name = Column(String)
    officer_father_name = Column(String)

    employer_name = Column(String)
    employer_address = Column(String)
    designation = Column(String)
    pan = Column(String, nullable=True)
    cit_tds_address = Column(String, nullable=True)
    cit_tds_city = Column(String, nullable=True)
    cit_tds_pincode = Column(String, nullable=True)

    # Shared-cache poisoning protection. Customer-submitted TAN records remain
    # private to their creator until an administrator verifies them.
    is_verified = Column(Boolean, default=False, nullable=False)
    created_by_user_id = Column(String, nullable=True, index=True)

    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    employees = relationship(
        "EmployeeDetail",
        back_populates="employer",
    )


class EmployeeDetail(Base):
    __tablename__ = "employee_details"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        unique=True,
        nullable=False,
    )

    tan_id = Column(
        String,
        ForeignKey("employers_by_tan.tan"),
        nullable=True,
    )

    name = Column(String)
    pan = Column(String, index=True)
    office_school_name = Column(String)

    user = relationship(
        "User",
        back_populates="employee_details",
    )

    employer = relationship(
        "EmployerCache",
        back_populates="employees",
    )


class MonthlyLedger(Base):
    """
    One payroll-ledger row for one exact month/year.

    Dynamic earning heads such as TA, Medical and other allowances are stored
    in line_items_json. Dynamic deductions are stored in deductions_json.
    """

    __tablename__ = "monthly_ledger"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "month_year",
            name="uq_monthly_ledger_user_month_year",
        ),
    )

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    month = Column(Integer, nullable=False)
    year = Column(Integer, nullable=False)

    # Canonical exact month identity, e.g. "2026-01".
    month_year = Column(String, nullable=False)

    # Admin-selected March-February cycle, e.g. "2025-26".
    financial_year = Column(String, nullable=False, index=True)

    basic_pay = Column(Float, default=0.0)
    da = Column(Float, default=0.0)
    hra = Column(Float, default=0.0)
    gross_salary = Column(Float, default=0.0)

    # TA, Medical, special allowance, arrear heads, etc.
    line_items_json = Column(JSON, default=dict)

    # GPF, GLI, professional tax, TDS and other deductions.
    deductions_json = Column(JSON, default=dict)

    is_auto_generated = Column(Boolean, default=False)

    # Typical values:
    # extracted / combined_period / arrear / auto_generated
    source = Column(String, nullable=False, default="extracted")

    # Preserve projection/manual-review audit information.
    note = Column(String, nullable=True)
    flags = Column(JSON, default=list)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user = relationship(
        "User",
        back_populates="ledgers",
    )


class Payment(Base):
    __tablename__ = "payments"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    amount = Column(Float, nullable=False)
    upi_txn_utr = Column(String, unique=True, index=True, nullable=True)

    # pending -> approved -> rejected / failed / refunded
    status = Column(String, default="pending")

    approved_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship(
        "User",
        back_populates="payments",
    )

    form16_generations = relationship(
        "Form16Generation",
        back_populates="payment",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="payment",
    )


class VisitorSession(Base):
    """
    Tracks one user journey through the Form-16 flow.

    visitor_id is the stable browser-level identifier used for unique-visitor
    analytics. id is one specific application/session journey. A returning
    browser can therefore have multiple journeys without inflating the unique
    visitor count.
    """

    __tablename__ = "visitor_sessions"

    id = Column(String, primary_key=True)

    visitor_id = Column(
        String,
        nullable=False,
        index=True,
    )

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    # Captured as soon as the user supplies contact information, even if the
    # application is abandoned before the full EmployeeDetail is completed.
    email = Column(String, nullable=True, index=True)
    mobile = Column(String, nullable=True)

    # Examples:
    # upload / review / ddo_details / payment / payment_wait / completed
    current_page = Column(String, nullable=True, index=True)
    current_step = Column(String, nullable=True)
    last_completed_step = Column(String, nullable=True)
    progress_percent = Column(Integer, default=0)

    # in_progress / payment_pending / payment_submitted / completed / abandoned
    application_status = Column(
        String,
        nullable=False,
        default="in_progress",
        index=True,
    )

    # not_started / pending / approved / rejected
    payment_status = Column(
        String,
        nullable=False,
        default="not_started",
        index=True,
    )

    # Only a whitelisted partial snapshot should be stored by main.py.
    # Never place passwords, raw PDF bytes, card details or UPI PINs here.
    form_snapshot_json = Column(JSON, default=dict)

    # Used for a secure resume link. main.py will generate a random token.
    resume_token = Column(
        String,
        unique=True,
        nullable=True,
        index=True,
    )

    # Service-email/reminder consent captured in the UI.
    email_contact_consent = Column(Boolean, default=False)

    # not_eligible / pending / sent / failed / cancelled
    reminder_status = Column(
        String,
        nullable=False,
        default="not_eligible",
        index=True,
    )

    reminder_due_at = Column(DateTime, nullable=True, index=True)
    reminder_sent_at = Column(DateTime, nullable=True)

    started_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    abandoned_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="visitor_sessions",
    )

    events = relationship(
        "VisitorEvent",
        back_populates="visitor_session",
        cascade="all, delete-orphan",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="visitor_session",
    )


class VisitorEvent(Base):
    """
    Append-only funnel/event history for analytics.

    Typical event_type values:
    page_view / form_started / contact_saved / step_completed /
    payment_submitted / payment_approved / payment_rejected /
    form16_generated / form16_downloaded
    """

    __tablename__ = "visitor_events"

    id = Column(String, primary_key=True)

    session_id = Column(
        String,
        ForeignKey("visitor_sessions.id"),
        nullable=False,
        index=True,
    )

    # Denormalised for fast unique-visitor/funnel queries.
    visitor_id = Column(String, nullable=False, index=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    event_type = Column(String, nullable=False, index=True)
    page_name = Column(String, nullable=True, index=True)
    step_name = Column(String, nullable=True)

    # Keep this metadata minimal and non-sensitive.
    event_data_json = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    visitor_session = relationship(
        "VisitorSession",
        back_populates="events",
    )


class Form16Generation(Base):
    """
    Permanent audit record for every Form 16 generation.

    source distinguishes normal paid-user generation from direct admin
    generation, so admin-generated documents never need fake payment records.
    """

    __tablename__ = "form16_generations"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    payment_id = Column(
        String,
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )

    financial_year = Column(String, nullable=False, index=True)
    assessment_year = Column(String, nullable=True)

    # user_payment / admin
    source = Column(String, nullable=False, index=True)

    # queued / generating / generated / failed
    status = Column(
        String,
        nullable=False,
        default="queued",
        index=True,
    )

    file_name = Column(String, nullable=True)
    storage_path = Column(String, nullable=True)
    pdf_blob = Column(LargeBinary, nullable=True)
    content_sha256 = Column(String, nullable=True)
    version_no = Column(Integer, default=1, nullable=False)
    is_latest = Column(Boolean, default=True, nullable=False)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    generated_at = Column(DateTime, nullable=True)

    download_count = Column(Integer, default=0)
    first_downloaded_at = Column(DateTime, nullable=True)
    last_downloaded_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="form16_generations",
    )

    payment = relationship(
        "Payment",
        back_populates="form16_generations",
    )

    email_deliveries = relationship(
        "EmailDelivery",
        back_populates="generation",
    )


class EmailDelivery(Base):
    """
    Email queue/audit table for both transactional and reminder emails.

    Typical email_type values:
    abandoned_reminder / payment_confirmed_form16 / admin_generated_form16
    """

    __tablename__ = "email_deliveries"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )

    visitor_session_id = Column(
        String,
        ForeignKey("visitor_sessions.id"),
        nullable=True,
        index=True,
    )

    payment_id = Column(
        String,
        ForeignKey("payments.id"),
        nullable=True,
        index=True,
    )

    generation_id = Column(
        String,
        ForeignKey("form16_generations.id"),
        nullable=True,
        index=True,
    )

    recipient_email = Column(String, nullable=False, index=True)
    email_type = Column(String, nullable=False, index=True)
    subject = Column(String, nullable=True)

    # queued / sending / sent / failed / cancelled
    status = Column(
        String,
        nullable=False,
        default="queued",
        index=True,
    )

    scheduled_for = Column(DateTime, nullable=True, index=True)
    attempt_count = Column(Integer, default=0)

    provider_message_id = Column(String, nullable=True)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    sent_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    user = relationship(
        "User",
        back_populates="email_deliveries",
    )

    visitor_session = relationship(
        "VisitorSession",
        back_populates="email_deliveries",
    )

    payment = relationship(
        "Payment",
        back_populates="email_deliveries",
    )

    generation = relationship(
        "Form16Generation",
        back_populates="email_deliveries",
    )



# =========================================================
# PRODUCTION INTEGRATIONS / AUDIT TABLES
# =========================================================

class IntegrationConfig(Base):
    """Non-secret configuration for an external integration.

    One row per provider, for example ``smtp``, ``razorpay`` or ``telegram``.
    Secrets never belong in ``config_json``; they are stored encrypted in
    IntegrationSecret.
    """

    __tablename__ = "integration_configs"

    id = Column(String, primary_key=True)
    provider = Column(String, nullable=False, unique=True, index=True)
    enabled = Column(Boolean, nullable=False, default=False, index=True)
    config_json = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class IntegrationSecret(Base):
    """Encrypted-at-rest credentials for external integrations.

    ``encrypted_value`` must contain ciphertext produced by backend encryption
    using a server-side master key such as APP_ENCRYPTION_KEY. The master key
    itself must never be stored in this table.
    """

    __tablename__ = "integration_secrets"

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "secret_name",
            name="uq_integration_secret_provider_name",
        ),
    )

    id = Column(String, primary_key=True)
    provider = Column(String, nullable=False, index=True)
    secret_name = Column(String, nullable=False)
    encrypted_value = Column(Text, nullable=False)
    masked_hint = Column(String, nullable=True)
    key_version = Column(Integer, nullable=False, default=1)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class GatewayPayment(Base):
    """Provider-specific payment details kept separate from manual UTR data.

    The existing Payment table remains the canonical payment/revenue record.
    This companion row stores gateway order/payment identifiers and verification
    state without forcing fake UTR values into Payment.upi_txn_utr.
    """

    __tablename__ = "gateway_payments"

    id = Column(String, primary_key=True)

    payment_id = Column(
        String,
        ForeignKey("payments.id"),
        nullable=False,
        unique=True,
        index=True,
    )

    provider = Column(String, nullable=False, default="razorpay", index=True)
    mode = Column(String, nullable=False, default="test")  # test / live
    currency = Column(String, nullable=False, default="INR")

    provider_order_id = Column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    provider_payment_id = Column(
        String,
        nullable=True,
        unique=True,
        index=True,
    )

    # created / pending / verified / failed / refunded
    status = Column(String, nullable=False, default="created", index=True)
    verified_at = Column(DateTime, nullable=True)
    failed_at = Column(DateTime, nullable=True)
    failure_reason = Column(String, nullable=True)

    # Minimal provider metadata only. Never store card number, CVV or UPI PIN.
    metadata_json = Column(JSON, default=dict)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    payment = relationship("Payment")


class PaymentWebhookEvent(Base):
    """Idempotency/audit record for payment-provider webhook events."""

    __tablename__ = "payment_webhook_events"

    id = Column(String, primary_key=True)

    provider = Column(String, nullable=False, index=True)
    provider_event_id = Column(
        String,
        nullable=False,
        unique=True,
        index=True,
    )
    event_type = Column(String, nullable=False, index=True)

    gateway_payment_id = Column(
        String,
        ForeignKey("gateway_payments.id"),
        nullable=True,
        index=True,
    )

    payload_hash = Column(String, nullable=True)
    event_data_json = Column(JSON, default=dict)

    # received / processed / ignored / failed
    status = Column(String, nullable=False, default="received", index=True)
    error_message = Column(String, nullable=True)

    received_at = Column(DateTime, default=datetime.utcnow, index=True)
    processed_at = Column(DateTime, nullable=True)

    gateway_payment = relationship("GatewayPayment")


class OperationIdempotency(Base):
    """Generic once-only guard for generation/email/payment side effects."""

    __tablename__ = "operation_idempotency"

    id = Column(String, primary_key=True)
    operation_type = Column(String, nullable=False, index=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    entity_type = Column(String, nullable=True, index=True)
    entity_id = Column(String, nullable=True, index=True)

    # started / completed / failed
    status = Column(String, nullable=False, default="started", index=True)
    result_json = Column(JSON, default=dict)
    error_message = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)


class ConsentRecord(Base):
    """Versioned consent/privacy notice audit without changing VisitorSession."""

    __tablename__ = "consent_records"

    id = Column(String, primary_key=True)

    user_id = Column(
        String,
        ForeignKey("users.id"),
        nullable=True,
        index=True,
    )
    visitor_session_id = Column(
        String,
        ForeignKey("visitor_sessions.id"),
        nullable=True,
        index=True,
    )

    consent_type = Column(String, nullable=False, index=True)
    notice_version = Column(String, nullable=False)
    granted = Column(Boolean, nullable=False, default=False)
    metadata_json = Column(JSON, default=dict)
    recorded_at = Column(DateTime, default=datetime.utcnow, index=True)


class AdminAuditLog(Base):
    """Append-only record of important admin actions."""

    __tablename__ = "admin_audit_logs"

    id = Column(String, primary_key=True)
    action = Column(String, nullable=False, index=True)
    entity_type = Column(String, nullable=True, index=True)
    entity_id = Column(String, nullable=True, index=True)

    # Minimal non-secret metadata only.
    details_json = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


# --- VetanKosh production additions ---
class AdminLoginChallenge(Base):
    __tablename__ = "admin_login_challenges"
    id = Column(String, primary_key=True)
    otp_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    ip_hash = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id = Column(String, primary_key=True)
    token_hash = Column(String, nullable=False, unique=True, index=True)
    ip_hash = Column(String, nullable=True)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    last_seen_at = Column(DateTime, default=datetime.utcnow)
    expires_at = Column(DateTime, nullable=False, index=True)
    revoked_at = Column(DateTime, nullable=True, index=True)

class Form16ArchiveAccess(Base):
    __tablename__ = "form16_archive_access"
    id = Column(String, primary_key=True)
    generation_id = Column(String, ForeignKey("form16_generations.id"), nullable=False, index=True)
    action = Column(String, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class ProfileAccessChallenge(Base):
    __tablename__ = "profile_access_challenges"
    id = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    otp_hash = Column(String, nullable=False)
    expires_at = Column(DateTime, nullable=False, index=True)
    attempts = Column(Integer, default=0, nullable=False)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class DurableJob(Base):
    """Small PostgreSQL-backed work queue; keeps CPU-heavy work out of web workers."""
    __tablename__ = "durable_jobs"
    __table_args__ = (UniqueConstraint("job_type", "dedupe_key", name="uq_durable_job_type_key"),)
    id = Column(String, primary_key=True)
    job_type = Column(String, nullable=False, index=True)
    dedupe_key = Column(String, nullable=False, index=True)
    payload_json = Column(JSON, default=dict)
    status = Column(String, nullable=False, default="queued", index=True)  # queued/processing/done/retry/failed
    attempt_count = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    available_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    locked_at = Column(DateTime, nullable=True, index=True)
    locked_by = Column(String, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class RefundRequest(Base):
    """User request raised when a delivered Form-16 working report appears incorrect."""
    __tablename__ = "refund_requests"
    id = Column(String, primary_key=True)
    payment_id = Column(String, ForeignKey("payments.id"), nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    generation_id = Column(String, ForeignKey("form16_generations.id"), nullable=True, index=True)
    request_type = Column(String, nullable=False, index=True)  # correction / refund
    reason = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="open", index=True)  # open/reviewing/corrected/refund_approved/refunded/rejected
    admin_note = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
