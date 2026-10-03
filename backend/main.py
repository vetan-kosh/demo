import os
import re
import uuid
import tempfile
import hashlib
import hmac
import time
import smtplib
import secrets
import shutil
import io
import threading
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from email.message import EmailMessage
from email.utils import formataddr

import requests

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    status,
    Request,
    BackgroundTasks,
    File,
    UploadFile,
    Form,
    Cookie,
    Response,
)

from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    StreamingResponse,
)

from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel, Field

from models import (
    SessionLocal,
    EmployerCache,
    MonthlyLedger,
    User,
    AdminSettings,
    Payment,
    EmployeeDetail,
    VisitorSession,
    VisitorEvent,
    Form16Generation,
    EmailDelivery,
    AdminLoginChallenge, AdminSession, Form16ArchiveAccess, ProfileAccessChallenge,
    GatewayPayment, PaymentWebhookEvent, DurableJob, RefundRequest,
)

from core.parser import load_and_parse_pdf


try:
    from pdf_generator import generate_form16_pdf
except ImportError:
    generate_form16_pdf = None


_PRODUCTION = os.getenv("APP_ENV", "development").strip().lower() == "production"
app = FastAPI(
    title="VetanKosh API",
    version="1.0",
    docs_url=None if _PRODUCTION else "/docs",
    redoc_url=None if _PRODUCTION else "/redoc",
    openapi_url=None if _PRODUCTION else "/openapi.json",
)

templates = Jinja2Templates(directory="templates")
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(self)"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; img-src 'self' data:; font-src 'self' data: https://fonts.gstatic.com; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; "
        "frame-src https://api.razorpay.com https://checkout.razorpay.com; "
        "connect-src 'self' https://api.razorpay.com"
    )
    if request.url.path.startswith(("/admin", "/api/profile", "/api/download")):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Pragma"] = "no-cache"
    if request_uses_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Lightweight per-process abuse throttling for the low-resource one-web-worker deployment.
# Put Nginx/edge rate limiting in front as the primary production layer.
_PUBLIC_RATE_LOCK = threading.Lock()
_PUBLIC_RATE = {}
_PUBLIC_RATE_RULES = {
    "/api/extract": (20, 60),
    "/api/profile/lookup": (20, 300),
    "/api/profile/request-otp": (5, 900),
    "/api/profile/verify-otp": (10, 900),
    "/api/payment/submit": (10, 300),
    "/api/payment/gateway/order": (10, 300),
    "/api/payment/gateway/verify": (20, 300),
    "/api/refund/request": (5, 900),
}

def _client_ip(request: Request) -> str:
    # Nginx overwrites X-Forwarded-For; never expose Uvicorn directly to Internet.
    return (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown"))

@app.middleware("http")
async def public_abuse_throttle(request: Request, call_next):
    rule = _PUBLIC_RATE_RULES.get(request.url.path)
    if rule and request.method.upper() in {"POST", "PUT", "PATCH"}:
        limit, window = rule; now = time.time(); key = (request.url.path, hashlib.sha256(_client_ip(request).encode()).hexdigest()[:24])
        with _PUBLIC_RATE_LOCK:
            hits = [t for t in _PUBLIC_RATE.get(key, []) if now - t < window]
            if len(hits) >= limit:
                return Response(content="Too many requests. Please try again later.", status_code=429, headers={"Retry-After": str(window)})
            hits.append(now); _PUBLIC_RATE[key] = hits
    return await call_next(request)

# =========================================================
# DATABASE
# =========================================================

def get_db():
    db = SessionLocal()

    try:
        yield db
    finally:
        db.close()


# =========================================================
# HELPERS
# =========================================================

def validate_financial_year(financial_year: str) -> str:
    """
    Expected format: YYYY-YY
    Example: 2026-27
    """

    if not financial_year:
        raise ValueError("Financial year is required.")

    financial_year = financial_year.strip()

    match = re.fullmatch(r"(\d{4})-(\d{2})", financial_year)

    if not match:
        raise ValueError(
            "Invalid financial year. Expected format YYYY-YY."
        )

    start_year = int(match.group(1))
    end_suffix = int(match.group(2))

    expected_suffix = (start_year + 1) % 100

    if end_suffix != expected_suffix:
        raise ValueError(
            "Financial year must contain consecutive years."
        )

    return financial_year


def get_active_financial_year(db: Session) -> str:
    settings = db.query(AdminSettings).first()

    if not settings or not settings.financial_year:
        raise HTTPException(
            status_code=400,
            detail="Admin has not selected the financial year yet.",
        )

    try:
        return validate_financial_year(settings.financial_year)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )


def month_year_string(year: int, month: int) -> str:
    if month < 1 or month > 12:
        raise ValueError("Month must be between 1 and 12.")

    return f"{int(year):04d}-{int(month):02d}"


def month_belongs_to_financial_year(
    month: int,
    year: int,
    financial_year: str,
) -> bool:

    financial_year = validate_financial_year(financial_year)

    start_year = int(financial_year[:4])
    end_year = start_year + 1

    # Payroll/Form-16 cycle:
    # March -> December = start year
    # January -> February = end year

    if 3 <= month <= 12:
        return year == start_year

    if month in (1, 2):
        return year == end_year

    return False


def get_slip_months(slip: dict) -> list:
    """
    Normalises parser period information into:
    [(month, year), ...]
    """

    period = slip.get("period") or {}
    months = period.get("months") or []

    result = []

    for item in months:
        if not isinstance(item, (list, tuple)):
            continue

        if len(item) < 2:
            continue

        try:
            month = int(item[0])
            year = int(item[1])
        except (TypeError, ValueError):
            continue

        if 1 <= month <= 12:
            result.append((month, year))

    return result



# =========================================================
# VISITOR / EMAIL / FORM-16 HELPERS
# =========================================================

PUBLIC_PAGE_STEPS = {
    "/": ("upload", "upload", 10),
    "/review": ("review", "review", 35),
    "/ddo-details": ("ddo_details", "ddo_details", 60),
    "/payment": ("payment", "payment", 80),
}

REMINDER_DELAY_HOURS = int(os.getenv("REMINDER_DELAY_HOURS", "6"))


def assessment_year_from_financial_year(financial_year: str) -> str:
    financial_year = validate_financial_year(financial_year)
    start_year = int(financial_year[:4])
    return f"{start_year + 1}-{(start_year + 2) % 100:02d}"


def normalise_email(value: Optional[str]) -> Optional[str]:
    value = (value or "").strip().lower()
    if not value:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+",
        value,
    ):
        raise ValueError("Valid email address is required.")

    return value


def normalise_mobile(value: Optional[str]) -> Optional[str]:
    value = re.sub(r"\D", "", value or "")

    if not value:
        return None

    if len(value) == 12 and value.startswith("91"):
        value = value[2:]

    if not re.fullmatch(r"[6-9]\d{9}", value):
        raise ValueError("Valid 10-digit Indian mobile number is required.")

    return value


def safe_snapshot(data: Optional[dict]) -> dict:
    data = data or {}
    allowed = {
        "name",
        "pan",
        "office_school_name",
        "tan_id",
    }

    result = {}
    for key in allowed:
        value = data.get(key)
        if value is not None:
            result[key] = str(value)[:500]

    return result


def add_visitor_event(
    db: Session,
    visitor_session: VisitorSession,
    event_type: str,
    page_name: Optional[str] = None,
    step_name: Optional[str] = None,
    event_data: Optional[dict] = None,
):
    event = VisitorEvent(
        id=str(uuid.uuid4()),
        session_id=visitor_session.id,
        visitor_id=visitor_session.visitor_id,
        user_id=visitor_session.user_id,
        event_type=event_type,
        page_name=page_name,
        step_name=step_name,
        event_data_json=event_data or {},
    )
    db.add(event)


def get_or_create_visitor_session(
    db: Session,
    visitor_id: Optional[str],
    journey_id: Optional[str],
) -> tuple[VisitorSession, str, str]:
    visitor_id = (visitor_id or "").strip() or str(uuid.uuid4())
    journey_id = (journey_id or "").strip()

    session = None
    if journey_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.id == journey_id)
            .first()
        )

    if not session:
        session = VisitorSession(
            id=str(uuid.uuid4()),
            visitor_id=visitor_id,
            current_page="upload",
            current_step="upload",
            progress_percent=10,
            application_status="in_progress",
            payment_status="not_started",
            resume_token=secrets.token_urlsafe(32),
            last_seen_at=datetime.utcnow(),
        )
        db.add(session)
        db.flush()
        add_visitor_event(
            db,
            session,
            "form_started",
            page_name="upload",
            step_name="upload",
        )

    return session, visitor_id, session.id


def find_active_visitor_session(
    db: Session,
    journey_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[VisitorSession]:
    if journey_id:
        session = (
            db.query(VisitorSession)
            .filter(VisitorSession.id == journey_id)
            .first()
        )
        if session:
            return session

    if user_id:
        return (
            db.query(VisitorSession)
            .filter(VisitorSession.user_id == user_id)
            .order_by(VisitorSession.last_seen_at.desc())
            .first()
        )

    return None


def require_journey_user(request: Request, db: Session, user_id: str) -> VisitorSession:
    """Authorize anonymous in-progress workflow access using the HttpOnly journey cookie.

    A client-supplied user_id is never sufficient. The cookie must point to the
    server-created visitor session already bound to that exact user.
    """
    journey_id = (request.cookies.get("form16_journey_id") or "").strip()
    if not journey_id or not user_id:
        raise HTTPException(status_code=401, detail="Workflow authorization required.")
    session = (db.query(VisitorSession)
        .filter(VisitorSession.id == journey_id, VisitorSession.user_id == user_id)
        .first())
    if not session:
        raise HTTPException(status_code=403, detail="This workflow does not own the requested record.")
    return session


def bind_new_user_to_journey(request: Request, db: Session) -> str:
    journey_id = (request.cookies.get("form16_journey_id") or "").strip()
    if not journey_id:
        raise HTTPException(status_code=401, detail="Workflow session is missing. Reload the upload page.")
    session = db.query(VisitorSession).filter(VisitorSession.id == journey_id).first()
    if not session:
        raise HTTPException(status_code=401, detail="Workflow session is invalid or expired.")
    # Reuse an already server-bound user on retry; otherwise issue a fresh ID.
    user_id = session.user_id or str(uuid.uuid4())
    session.user_id = user_id
    if not db.query(User).filter(User.id == user_id).first():
        db.add(User(id=user_id))
    session.last_seen_at = datetime.utcnow()
    db.commit()
    return user_id


def sync_visitor_payment_state(
    db: Session,
    user_id: str,
    payment_status: str,
    application_status: Optional[str] = None,
):
    sessions = (
        db.query(VisitorSession)
        .filter(VisitorSession.user_id == user_id)
        .all()
    )

    now = datetime.utcnow()

    for session in sessions:
        session.payment_status = payment_status
        session.last_seen_at = now

        if application_status:
            session.application_status = application_status

        if payment_status == "approved":
            session.reminder_status = "cancelled"
            session.reminder_due_at = None
            session.completed_at = now
            session.current_page = "completed"
            session.current_step = "completed"
            session.last_completed_step = "payment"
            session.progress_percent = 100

        add_visitor_event(
            db,
            session,
            f"payment_{payment_status}",
            page_name=session.current_page,
            step_name=session.current_step,
        )


def smtp_is_configured() -> bool:
    return bool(
        os.getenv("SMTP_USERNAME")
        and os.getenv("SMTP_PASSWORD")
    )


def send_email_message(
    recipient: str,
    subject: str,
    body: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> Optional[str]:
    if not smtp_is_configured():
        raise RuntimeError(
            "Email is not configured. Set SMTP_USERNAME and SMTP_PASSWORD."
        )

    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "587"))
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    from_name = os.getenv("SMTP_FROM_NAME", "Form 16 Support").strip()

    message = EmailMessage()
    message["From"] = formataddr((from_name, username))
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)

    if attachment_path:
        with open(attachment_path, "rb") as file_obj:
            pdf_bytes = file_obj.read()

        message.add_attachment(
            pdf_bytes,
            maintype="application",
            subtype="pdf",
            filename=attachment_name or "Form16.pdf",
        )

    with smtplib.SMTP(host, port, timeout=20) as server:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, password)
        response = server.send_message(message)

    return "smtp-accepted" if not response else str(response)


def queue_email_delivery(
    db: Session,
    recipient_email: str,
    email_type: str,
    subject: str,
    user_id: Optional[str] = None,
    visitor_session_id: Optional[str] = None,
    payment_id: Optional[str] = None,
    generation_id: Optional[str] = None,
    scheduled_for: Optional[datetime] = None,
) -> EmailDelivery:
    delivery = EmailDelivery(
        id=str(uuid.uuid4()),
        user_id=user_id,
        visitor_session_id=visitor_session_id,
        payment_id=payment_id,
        generation_id=generation_id,
        recipient_email=recipient_email,
        email_type=email_type,
        subject=subject,
        status="queued",
        scheduled_for=scheduled_for,
        attempt_count=0,
    )
    db.add(delivery)
    db.flush()
    return delivery


def send_delivery_now(
    db: Session,
    delivery: EmailDelivery,
    body: str,
    attachment_path: Optional[str] = None,
    attachment_name: Optional[str] = None,
) -> bool:
    delivery.status = "sending"
    delivery.attempt_count = int(delivery.attempt_count or 0) + 1
    delivery.updated_at = datetime.utcnow()
    db.commit()

    try:
        provider_message_id = send_email_message(
            recipient=delivery.recipient_email,
            subject=delivery.subject or "Form 16",
            body=body,
            attachment_path=attachment_path,
            attachment_name=attachment_name,
        )

        delivery.status = "sent"
        delivery.provider_message_id = provider_message_id
        delivery.sent_at = datetime.utcnow()
        delivery.error_message = None
        delivery.updated_at = datetime.utcnow()
        db.commit()
        return True

    except Exception as exc:
        delivery.status = "failed"
        delivery.error_message = str(exc)[:1000]
        delivery.updated_at = datetime.utcnow()
        db.commit()
        print(f"Email delivery failed: {exc}")
        return False



def _projection_numeric_map(values: Optional[dict]) -> dict:
    """Return finite numeric ordinary-payroll values, excluding arrear_* metadata."""
    result = {}

    for raw_key, raw_value in (values or {}).items():
        key = re.sub(r"[^a-z0-9_]+", "_", str(raw_key).strip().lower()).strip("_")
        if not key or key.startswith("arrear_"):
            continue

        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        if value != value:  # NaN guard
            continue

        result[key] = round(value, 2)

    return result


def _projection_arrear_gross(flags: Optional[list]) -> float:
    """Recover arrear gross merged into a regular month so it is never projected forward."""
    total = 0.0

    for raw_flag in (flags or []):
        flag = str(raw_flag)
        if not flag.startswith("arrear_gross:"):
            continue

        parts = flag.split(":", 2)
        if len(parts) < 2:
            continue

        try:
            total += float(parts[1])
        except (TypeError, ValueError):
            continue

    return round(total, 2)


CLERK_INCREMENT_CONFIRM_BASIC = {19900.0: 20500.0, 21700.0: 22400.0, 25500.0: 26300.0, 29200.0: 30100.0}
CLERK_INCREMENT_AUTO_BASIC = {20500.0: 21100.0, 22400.0: 23100.0, 26300.0: 27100.0, 30100.0: 31000.0}

def _flag_value(flags: Optional[list], prefix: str) -> Optional[str]:
    for raw in flags or []:
        value = str(raw)
        if value.startswith(prefix):
            return value[len(prefix):]
    return None

def _clerk_january_basic(source_row) -> tuple[float, str]:
    """Apply the VetanKosh Clerk increment rule to a January projection.

    Starting cells require an explicit user Yes/No captured during review.
    Second cells auto-increment without prompting. Other pay values are left
    to the existing projection behavior.
    """
    current = round(float(source_row.basic_pay or 0), 2)
    flags = list(source_row.flags or [])
    designation = (_flag_value(flags, "employee_designation:") or "").strip().lower()
    if "clerk" not in designation:
        return current, "not_clerk"

    if current in CLERK_INCREMENT_AUTO_BASIC:
        return CLERK_INCREMENT_AUTO_BASIC[current], "auto"

    if current in CLERK_INCREMENT_CONFIRM_BASIC:
        choice = (_flag_value(flags, "clerk_increment_choice:") or "").strip().lower()
        if choice == "yes":
            return CLERK_INCREMENT_CONFIRM_BASIC[current], "confirmed_yes"
        if choice == "no":
            return current, "confirmed_no"
        # Fail safe: never invent an increment when the required answer is missing.
        return current, "confirmation_missing"

    return current, "not_special_cell"

def _ensure_missing_jan_feb_projections(
    db: Session,
    user_id: str,
    financial_year: str,
) -> None:
    """
    Materialise only the two allowed projection months for this project cycle:
    January and February of the FY end-year.

    Rules:
      * Never invent March-December rows.
      * Never overwrite an actual January/February row.
      * January is projected only from an ordinary December row.
      * February is projected only from an ordinary January row (actual or auto).
      * Arrear amounts merged into the source month are stripped before projection.
      * The projected row is persisted, so PDF/email/admin generation all use the
        same audited ledger. A later actual slip replaces it through /api/ledger/save.
    """
    financial_year = validate_financial_year(financial_year)
    end_year = int(financial_year[:4]) + 1

    def fetch_rows():
        return (
            db.query(MonthlyLedger)
            .filter(
                MonthlyLedger.user_id == user_id,
                MonthlyLedger.financial_year == financial_year,
            )
            .all()
        )

    def find_row(rows, month, year):
        return next(
            (
                row for row in rows
                if row.month == month and row.year == year
            ),
            None,
        )

    def is_projection_source(row) -> bool:
        if not row:
            return False

        source = (row.source or "").strip().lower()
        if source in {"arrear", "combined_period"}:
            return False

        regular_gross = round(
            float(row.gross_salary or 0)
            - _projection_arrear_gross(row.flags),
            2,
        )

        return regular_gross > 0

    def create_projection(source_row, target_month, target_year, target_label):
        line_items = _projection_numeric_map(source_row.line_items_json)
        deductions = _projection_numeric_map(source_row.deductions_json)

        # Keep canonical Basic/DA/HRA synchronized with the copied line-item map.
        source_basic_pay = round(float(source_row.basic_pay or 0), 2)
        basic_pay = source_basic_pay
        increment_mode = "unchanged"
        if target_month == 1:
            basic_pay, increment_mode = _clerk_january_basic(source_row)
        da = round(float(source_row.da or 0), 2)
        hra = round(float(source_row.hra or 0), 2)

        if "basic_pay" in line_items:
            line_items["basic_pay"] = basic_pay
        if "da" in line_items:
            line_items["da"] = da
        if "hra" in line_items:
            line_items["hra"] = hra

        regular_gross = round(
            max(
                0.0,
                float(source_row.gross_salary or 0)
                - _projection_arrear_gross(source_row.flags),
            )
            + (basic_pay - source_basic_pay),
            2,
        )

        source_month_year = source_row.month_year or month_year_string(
            source_row.year,
            source_row.month,
        )
        target_month_year = month_year_string(target_year, target_month)

        note = (
            f"{target_label} auto-projected from ordinary payroll month "
            f"{source_month_year}. Actual {target_label} salary slip, when uploaded, "
            "must replace this projection. Arrear amounts are never carried forward."
        )

        flags = [
            f"auto_projection:{target_month_year}",
            f"projection_basis:{source_month_year}",
            "projection_actual_slip_overrides",
        ]

        if target_month == 1:
            flags.append(f"january_clerk_increment:{increment_mode}")
            if basic_pay == source_basic_pay:
                flags.append(
                    "january_increment_not_invented:carried_forward_from_december"
                )
            else:
                flags.append(
                    f"january_basic_increment:{source_basic_pay:.0f}->{basic_pay:.0f}"
                )
        else:
            flags.append(
                "february_tds_carried_from_january:final_tax_settlement_review"
            )

        projected = MonthlyLedger(
            id=str(uuid.uuid4()),
            user_id=user_id,
            month=target_month,
            year=target_year,
            month_year=target_month_year,
            financial_year=financial_year,
            basic_pay=basic_pay,
            da=da,
            hra=hra,
            gross_salary=regular_gross,
            source="auto_generated",
            is_auto_generated=True,
            line_items_json=line_items,
            deductions_json=deductions,
            note=note,
            flags=flags,
        )

        db.add(projected)
        try:
            db.commit()
        except IntegrityError:
            # A concurrent request may have created the same projection or an
            # actual slip may have arrived. In either case, keep the winner.
            db.rollback()

    rows = fetch_rows()
    if not rows:
        return

    # January belongs to the FY end-year and is only projected from December.
    january = find_row(rows, 1, end_year)
    if january is None:
        december = find_row(rows, 12, end_year - 1)
        if is_projection_source(december):
            create_projection(december, 1, end_year, "January")
            rows = fetch_rows()

    # February is the final month of this March->February payroll cycle.
    february = find_row(rows, 2, end_year)
    if february is None:
        january = find_row(rows, 1, end_year)
        if is_projection_source(january):
            create_projection(january, 2, end_year, "February")


def build_form16_payload(
    db: Session,
    user_id: str,
    financial_year: str,
):
    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=400,
            detail="Employee details are incomplete.",
        )

    if not employee.tan_id:
        raise HTTPException(
            status_code=400,
            detail="Employer TAN is missing.",
        )

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == employee.tan_id)
        .first()
    )

    if not employer:
        raise HTTPException(
            status_code=400,
            detail="Employer details are missing.",
        )

    _ensure_missing_jan_feb_projections(
        db,
        user_id,
        financial_year,
    )

    ledger_rows = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id == user_id,
            MonthlyLedger.financial_year == financial_year,
        )
        .all()
    )

    if not ledger_rows:
        raise HTTPException(
            status_code=400,
            detail=f"No salary ledger found for FY {financial_year}.",
        )

    payroll_order = {
        3: 1, 4: 2, 5: 3, 6: 4, 7: 5, 8: 6,
        9: 7, 10: 8, 11: 9, 12: 10, 1: 11, 2: 12,
    }

    ledger_rows.sort(
        key=lambda row: (
            payroll_order.get(row.month, 99),
            row.year,
        )
    )

    formatted_ledger = []
    for row in ledger_rows:
        formatted_ledger.append(
            {
                "month": row.month,
                "year": row.year,
                "month_year": row.month_year,
                "financial_year": row.financial_year,
                "basic_pay": row.basic_pay or 0,
                "da": row.da or 0,
                "hra": row.hra or 0,
                "gross_salary": row.gross_salary or 0,
                "line_items": row.line_items_json or {},
                "deductions": row.deductions_json or {},
                "source": row.source,
                "is_auto_generated": row.is_auto_generated,
                "note": row.note,
                "flags": row.flags or [],
            }
        )

    return employee, employer, formatted_ledger


def generate_form16_for_record(
    db: Session,
    generation: Form16Generation,
) -> tuple[str, str]:
    if generate_form16_pdf is None:
        raise HTTPException(
            status_code=500,
            detail="PDF generator is not available.",
        )

    generation.status = "generating"
    db.commit()

    try:
        employee, employer, formatted_ledger = build_form16_payload(
            db,
            generation.user_id,
            generation.financial_year,
        )

        pdf_path = generate_form16_pdf(
            employee=employee,
            employer=employer,
            ledger=formatted_ledger,
            financial_year=generation.financial_year,
            tds_details={
                "cit_tds_address": getattr(employer, "cit_tds_address", "") or "",
                "cit_tds_city": getattr(employer, "cit_tds_city", "") or "",
                "cit_tds_pincode": getattr(employer, "cit_tds_pincode", "") or "",
            },
        )

        if not pdf_path or not os.path.exists(pdf_path):
            raise RuntimeError("Generated PDF file was not found.")

        safe_name = re.sub(
            r"[^A-Za-z0-9_-]+",
            "_",
            employee.name or "Employee",
        ).strip("_")

        filename = (
            f"Form_16_{safe_name}_FY_"
            f"{generation.financial_year}.pdf"
        )

        archive_root = os.path.abspath(os.getenv("FORM16_ARCHIVE_DIR", "./private_form16_archive"))
        os.makedirs(archive_root, mode=0o700, exist_ok=True)
        user_dir = os.path.join(archive_root, generation.user_id)
        os.makedirs(user_dir, mode=0o700, exist_ok=True)
        archive_name = f"{generation.id}_{filename}"
        archive_path = os.path.join(user_dir, archive_name)
        shutil.copy2(pdf_path, archive_path)
        with open(archive_path, "rb") as fh:
            archived_bytes = fh.read()
            digest = hashlib.sha256(archived_bytes).hexdigest()

        previous = db.query(Form16Generation).filter(
            Form16Generation.user_id == generation.user_id,
            Form16Generation.financial_year == generation.financial_year,
            Form16Generation.id != generation.id,
            Form16Generation.status == "generated",
        ).all()
        generation.version_no = max([int(x.version_no or 1) for x in previous] + [0]) + 1
        for old_generation in previous:
            old_generation.is_latest = False
        generation.status = "generated"
        generation.file_name = filename
        generation.storage_path = archive_path
        generation.pdf_blob = archived_bytes
        generation.content_sha256 = digest
        generation.is_latest = True
        generation.generated_at = datetime.utcnow()
        generation.error_message = None
        db.commit()

        return archive_path, filename

    except HTTPException:
        generation.status = "failed"
        generation.error_message = "Form 16 data validation failed."
        db.commit()
        raise

    except Exception as exc:
        generation.status = "failed"
        generation.error_message = str(exc)[:1000]
        db.commit()
        raise HTTPException(
            status_code=500,
            detail=f"PDF generation failed: {exc}",
        )


def get_or_create_generation(
    db: Session,
    user_id: str,
    financial_year: str,
    source: str,
    payment_id: Optional[str] = None,
) -> Form16Generation:
    query = db.query(Form16Generation).filter(
        Form16Generation.user_id == user_id,
        Form16Generation.financial_year == financial_year,
        Form16Generation.source == source,
    )

    if payment_id:
        query = query.filter(Form16Generation.payment_id == payment_id)

    existing = (
        query.order_by(Form16Generation.created_at.desc()).first()
    )

    if existing and existing.status in {"generated", "generating", "queued"}:
        return existing

    generation = Form16Generation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        payment_id=payment_id,
        financial_year=financial_year,
        assessment_year=assessment_year_from_financial_year(financial_year),
        source=source,
        status="queued",
    )
    db.add(generation)
    db.flush()
    return generation


def enqueue_form16_job(db: Session, generation: Form16Generation, email_type: str) -> DurableJob:
    key = generation.id
    existing = db.query(DurableJob).filter(DurableJob.job_type == "form16_generate_email", DurableJob.dedupe_key == key).first()
    if existing:
        if existing.status == "failed":
            existing.status = "retry"
            existing.available_at = datetime.utcnow()
            existing.last_error = None
        return existing
    job = DurableJob(id=str(uuid.uuid4()), job_type="form16_generate_email", dedupe_key=key, payload_json={"generation_id": generation.id, "email_type": email_type}, status="queued", available_at=datetime.utcnow())
    db.add(job); db.flush(); return job

def send_form16_email_for_generation(
    generation_id: str,
    email_type: str,
):
    db = SessionLocal()

    try:
        generation = (
            db.query(Form16Generation)
            .filter(Form16Generation.id == generation_id)
            .first()
        )

        if not generation:
            return

        user = (
            db.query(User)
            .filter(User.id == generation.user_id)
            .first()
        )

        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == generation.user_id)
            .first()
        )

        if not user or not user.email or not employee:
            return

        if generation.status == "generated" and generation.storage_path and os.path.isfile(generation.storage_path):
            pdf_path, filename = generation.storage_path, (generation.file_name or "Form16.pdf")
        elif generation.status == "generated" and generation.pdf_blob:
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
            tmp.write(generation.pdf_blob); tmp.close(); pdf_path, filename = tmp.name, (generation.file_name or "Form16.pdf")
        else:
            pdf_path, filename = generate_form16_for_record(db, generation)

        if email_type == "payment_confirmed_form16":
            subject = (
                f"Payment Confirmed – Your Form 16 for "
                f"FY {generation.financial_year}"
            )
            body = (
                f"Dear {employee.name or 'Employee'},\n\n"
                "Your payment has been received and verified successfully.\n\n"
                f"Your Form 16 for Financial Year "
                f"{generation.financial_year} has now been generated and "
                "is attached with this email.\n\n"
                "Please keep this document safely for your income-tax "
                "and official records.\n\n"
                "Payment Status: Verified\n"
                f"Financial Year: {generation.financial_year}\n"
                f"PAN: {employee.pan or 'N/A'}\n\n"
                "Thank you for using our Form 16 service.\n\n"
                "Regards,\nForm 16 Support Team"
            )
        else:
            subject = (
                f"Form 16 Generated by Administrator – "
                f"FY {generation.financial_year}"
            )
            body = (
                f"Dear {employee.name or 'Employee'},\n\n"
                f"Your Form 16 for Financial Year "
                f"{generation.financial_year} has been generated directly "
                "by the authorised administrator and is attached with this "
                "email.\n\n"
                "No payment verification was required for this "
                "administrative generation.\n\n"
                "Generation Type: Administrator Generated\n"
                f"Financial Year: {generation.financial_year}\n"
                f"PAN: {employee.pan or 'N/A'}\n\n"
                "Please retain the attached document for your official "
                "and income-tax records.\n\n"
                "Regards,\nForm 16 Administration Team"
            )

        delivery = (db.query(EmailDelivery).filter(EmailDelivery.generation_id == generation.id, EmailDelivery.email_type == email_type).order_by(EmailDelivery.created_at.desc()).first())
        if delivery and delivery.status == "sent":
            return
        if not delivery:
            delivery = queue_email_delivery(db, recipient_email=user.email, email_type=email_type, subject=subject, user_id=user.id, payment_id=generation.payment_id, generation_id=generation.id)
        else:
            delivery.status = "queued"; delivery.subject = subject; delivery.error_message = None
        db.commit()

        send_delivery_now(
            db,
            delivery,
            body=body,
            attachment_path=pdf_path,
            attachment_name=filename,
        )

    except Exception as exc:
        print(f"Form 16 email task failed: {exc}")
        raise

    finally:
        db.close()


def process_due_reminders(limit: int = 50) -> dict:
    db = SessionLocal()
    now = datetime.utcnow()
    sent = 0
    failed = 0
    skipped = 0

    try:
        sessions = (
            db.query(VisitorSession)
            .filter(
                VisitorSession.reminder_status == "pending",
                VisitorSession.reminder_due_at.isnot(None),
                VisitorSession.reminder_due_at <= now,
                VisitorSession.email.isnot(None),
                VisitorSession.email_contact_consent.is_(True),
                VisitorSession.payment_status != "approved",
            )
            .order_by(VisitorSession.reminder_due_at.asc())
            .limit(limit)
            .all()
        )

        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).rstrip("/")

        for session in sessions:
            if not session.resume_token:
                skipped += 1
                continue

            resume_url = (
                f"{base_url}/resume/{session.resume_token}"
                if base_url
                else f"/resume/{session.resume_token}"
            )

            subject = "Complete Your Form 16 Application"
            body = (
                "Hello,\n\n"
                "Your Form 16 application is still incomplete. "
                "You can continue from where you left off using the "
                "resume link below:\n\n"
                f"{resume_url}\n\n"
                f"Last completed step: "
                f"{session.last_completed_step or 'Application started'}\n\n"
                "If you have already completed the process, please ignore "
                "this email.\n\n"
                "Regards,\nForm 16 Support Team"
            )

            delivery = queue_email_delivery(
                db,
                recipient_email=session.email,
                email_type="abandoned_reminder",
                subject=subject,
                user_id=session.user_id,
                visitor_session_id=session.id,
            )
            db.commit()

            ok = send_delivery_now(
                db,
                delivery,
                body=body,
            )

            if ok:
                session.reminder_status = "sent"
                session.reminder_sent_at = datetime.utcnow()
                session.application_status = (
                    "abandoned"
                    if session.application_status == "in_progress"
                    else session.application_status
                )
                session.abandoned_at = (
                    session.abandoned_at or datetime.utcnow()
                )
                sent += 1
            else:
                session.reminder_status = "failed"
                failed += 1

            db.commit()

        return {
            "sent": sent,
            "failed": failed,
            "skipped": skipped,
        }

    finally:
        db.close()


@app.middleware("http")
async def visitor_tracking_middleware(request: Request, call_next):
    path = request.url.path

    if (
        path.startswith("/admin")
        or path.startswith("/api")
        or path.startswith("/static")
        or path.startswith("/docs")
        or path.startswith("/openapi")
        or path == "/favicon.ico"
    ):
        return await call_next(request)

    visitor_id = request.cookies.get("form16_visitor_id")
    journey_id = request.cookies.get("form16_journey_id")

    db = SessionLocal()
    session = None
    new_visitor_id = visitor_id
    new_journey_id = journey_id

    try:
        session, new_visitor_id, new_journey_id = (
            get_or_create_visitor_session(
                db,
                visitor_id,
                journey_id,
            )
        )

        page_info = PUBLIC_PAGE_STEPS.get(path)
        if page_info:
            page_name, step_name, progress = page_info
            session.current_page = page_name
            session.current_step = step_name
            session.progress_percent = max(
                int(session.progress_percent or 0),
                progress,
            )
            session.last_seen_at = datetime.utcnow()

            # SECURITY: never bind a journey to a user_id supplied in a URL.
            # The server binds the journey when it issues the user ID after extraction.

            add_visitor_event(
                db,
                session,
                "page_view",
                page_name=page_name,
                step_name=step_name,
            )

        db.commit()

    except Exception as exc:
        db.rollback()
        print(f"Visitor tracking failed: {exc}")

    finally:
        db.close()

    response = await call_next(request)

    if new_visitor_id and new_visitor_id != visitor_id:
        response.set_cookie(
            "form16_visitor_id",
            new_visitor_id,
            max_age=365 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=request_uses_https(request),
        )

    if new_journey_id and new_journey_id != journey_id:
        response.set_cookie(
            "form16_journey_id",
            new_journey_id,
            max_age=30 * 24 * 60 * 60,
            httponly=True,
            samesite="lax",
            secure=request_uses_https(request),
        )

    return response




class RefundRequestSchema(BaseModel):
    payment_id: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., min_length=3, max_length=254)
    request_type: str = Field(..., min_length=1, max_length=20)
    reason: str = Field(..., min_length=10, max_length=2000)


@app.post("/api/refund/request")
def create_refund_request(data: RefundRequestSchema, db: Session = Depends(get_db)):
    request_type = data.request_type.strip().lower()
    if request_type not in {"correction", "refund"}:
        raise HTTPException(status_code=400, detail="Choose correction or refund.")

    payment = db.query(Payment).filter(Payment.id == data.payment_id.strip()).first()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment record not found.")
    user = db.query(User).filter(User.id == payment.user_id).first()
    try:
        request_email = normalise_email(data.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not user or not user.email or normalise_email(user.email) != request_email:
        raise HTTPException(status_code=404, detail="Payment record not found for this email.")
    if payment.status not in {"approved", "refunded"}:
        raise HTTPException(status_code=409, detail="Only an approved payment can enter this review process.")
    if payment.status == "refunded":
        raise HTTPException(status_code=409, detail="This payment is already marked refunded.")

    generation = (db.query(Form16Generation)
        .filter(Form16Generation.payment_id == payment.id)
        .order_by(Form16Generation.created_at.desc()).first())
    if not generation or generation.status != "generated":
        raise HTTPException(status_code=409, detail="A generated Form 16 report is required before raising this request.")

    existing = (db.query(RefundRequest)
        .filter(RefundRequest.payment_id == payment.id, RefundRequest.status.in_(["open", "reviewing", "refund_approved"]))
        .order_by(RefundRequest.created_at.desc()).first())
    if existing:
        return {"request_id": existing.id, "status": existing.status, "message": "An active review request already exists for this payment."}

    rr = RefundRequest(
        id=str(uuid.uuid4()), payment_id=payment.id, user_id=payment.user_id,
        generation_id=generation.id, request_type=request_type, reason=data.reason.strip(), status="open",
    )
    db.add(rr); db.commit()

    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    if admin_email:
        try:
            send_email_message(
                admin_email,
                f"VetanKosh {request_type.title()} Request",
                f"Request ID: {rr.id}\nPayment ID: {payment.id}\nChoice: {request_type}\nReason: {rr.reason}\n\nReview this request in the operator workflow before taking action.",
            )
        except Exception as exc:
            print(f"Refund/correction request email failed: {exc}")

    return {
        "request_id": rr.id, "status": rr.status,
        "message": ("Correction request received. Admin will review the report and prepare a corrected version where appropriate." if request_type == "correction" else "Refund request received. Admin will review whether the delivered report was incorrect and process an eligible refund through the applicable payment channel."),
    }

# =========================================================
# ADMIN SESSION HELPERS
# =========================================================

ADMIN_SESSION_MAX_AGE = 8 * 60 * 60


def _admin_username() -> str:
    return os.getenv("ADMIN_USERNAME", "").strip()


def _admin_password() -> str:
    return os.getenv("ADMIN_PASSWORD", "").strip()


def _admin_session_secret() -> str:
    # For production, set ADMIN_SESSION_SECRET in Render.
    secret = os.getenv("ADMIN_SESSION_SECRET", "").strip()
    if not secret:
        raise RuntimeError("ADMIN_SESSION_SECRET is required.")
    return secret


def _token_hash(token: str) -> str:
    return hmac.new(_admin_session_secret().encode(), token.encode(), hashlib.sha256).hexdigest()

def create_admin_session_token(request: Optional[Request] = None) -> str:
    token = secrets.token_urlsafe(48)
    now = datetime.utcnow()
    db = SessionLocal()
    try:
        active = db.query(AdminSession).filter(AdminSession.revoked_at.is_(None), AdminSession.expires_at > now).order_by(AdminSession.created_at.asc()).all()
        while len(active) >= 2:
            active.pop(0).revoked_at = now
        db.add(AdminSession(id=str(uuid.uuid4()), token_hash=_token_hash(token), ip_hash=_request_ip_hash(request) if request else None, user_agent=(request.headers.get("user-agent", "")[:500] if request else None), expires_at=now + timedelta(seconds=ADMIN_SESSION_MAX_AGE)))
        db.commit()
    finally:
        db.close()
    return token

def is_valid_admin_session(token: Optional[str]) -> bool:
    if not token:
        return False
    db = SessionLocal(); now = datetime.utcnow()
    try:
        row = db.query(AdminSession).filter(AdminSession.token_hash == _token_hash(token), AdminSession.revoked_at.is_(None), AdminSession.expires_at > now).first()
        if not row: return False
        row.last_seen_at = now; db.commit(); return True
    finally:
        db.close()

def require_admin_session(admin_session: Optional[str]) -> None:
    if not is_valid_admin_session(admin_session):
        raise HTTPException(status_code=401, detail="Admin authentication required.")

def _request_ip_hash(request: Optional[Request]) -> str:
    if not request: return ""
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else ""))
    return hashlib.sha256((ip + _admin_session_secret()).encode()).hexdigest()[:24]

def _otp_hash(challenge_id: str, otp: str) -> str:
    return hmac.new(_admin_session_secret().encode(), f"{challenge_id}:{otp}".encode(), hashlib.sha256).hexdigest()

def request_uses_https(request: Request) -> bool:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return (
        request.url.scheme == "https"
        or forwarded_proto.lower() == "https"
    )


# =========================================================
# ADMIN LOGIN RATE LIMIT + CSRF
# =========================================================

_ADMIN_LOGIN_WINDOW_SECONDS = 15 * 60
_ADMIN_LOGIN_MAX_FAILURES = 5
_ADMIN_LOGIN_LOCK_SECONDS = 15 * 60
_admin_login_attempts = {}
_admin_login_lock = threading.Lock()


def _login_rate_key(request: Request) -> str:
    return _request_ip_hash(request) or "unknown"


def _login_is_locked(request: Request) -> bool:
    now = time.time(); key = _login_rate_key(request)
    with _admin_login_lock:
        row = _admin_login_attempts.get(key)
        if not row:
            return False
        if row.get("locked_until", 0) > now:
            return True
        if now - row.get("window_start", now) > _ADMIN_LOGIN_WINDOW_SECONDS:
            _admin_login_attempts.pop(key, None)
    return False


def _record_login_failure(request: Request) -> None:
    now = time.time(); key = _login_rate_key(request)
    with _admin_login_lock:
        row = _admin_login_attempts.get(key)
        if not row or now - row.get("window_start", now) > _ADMIN_LOGIN_WINDOW_SECONDS:
            row = {"window_start": now, "failures": 0, "locked_until": 0}
        row["failures"] += 1
        if row["failures"] >= _ADMIN_LOGIN_MAX_FAILURES:
            row["locked_until"] = now + _ADMIN_LOGIN_LOCK_SECONDS
        _admin_login_attempts[key] = row


def _clear_login_failures(request: Request) -> None:
    with _admin_login_lock:
        _admin_login_attempts.pop(_login_rate_key(request), None)


def _new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _csrf_ok(request: Request, supplied: Optional[str]) -> bool:
    cookie = request.cookies.get("csrf_token", "")
    return bool(cookie and supplied and hmac.compare_digest(cookie, supplied))


@app.middleware("http")
async def admin_csrf_guard(request: Request, call_next):
    # Login/OTP forms validate their form token in their endpoint. Authenticated
    # admin state-changing requests use the double-submit cookie/header pattern.
    path = request.url.path
    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
        protected = (path.startswith("/api/admin/") or path.startswith("/admin/")) and path not in {"/admin/login", "/admin/otp"}
        if protected and request.cookies.get("admin_session"):
            supplied = request.headers.get("x-csrf-token")
            if not supplied and "application/x-www-form-urlencoded" in request.headers.get("content-type", ""):
                form = await request.form(); supplied = str(form.get("csrf_token") or "")
            if not _csrf_ok(request, supplied):
                return Response(content="CSRF validation failed.", status_code=403)
    return await call_next(request)

# =========================================================
# PYDANTIC SCHEMAS
# =========================================================

class EmployerSchema(BaseModel):
    tan: str = Field(..., example="RNCEDNK35")
    user_id: Optional[str] = None

    officer_name: str
    officer_father_name: str

    employer_name: str
    employer_address: str
    designation: str

    pan: Optional[str] = None
    cit_tds_address: Optional[str] = None
    cit_tds_city: Optional[str] = None
    cit_tds_pincode: Optional[str] = None


class LedgerSchema(BaseModel):
    user_id: str

    month: int = Field(..., ge=1, le=12)
    year: int

    basic_pay: float = 0
    da: float = 0
    hra: float = 0
    gross_salary: float = 0

    source: str = "extracted"

    line_items: Dict[str, Any] = Field(default_factory=dict)
    deductions: Dict[str, Any] = Field(default_factory=dict)

    note: Optional[str] = None
    flags: list[str] = Field(default_factory=list)


class ArrearLedgerSchema(BaseModel):
    user_id: str

    # Arrear belongs to the Form-16 cycle in which it was actually
    # paid/disbursed, not necessarily the old period printed on the bill.
    payment_month: int = Field(..., ge=1, le=12)
    payment_year: int

    original_period: str
    gross_salary: float = Field(default=0, ge=0)

    line_items: Dict[str, Any] = Field(default_factory=dict)
    deductions: Dict[str, Any] = Field(default_factory=dict)

    bill_no: Optional[str] = None
    note: Optional[str] = None
    flags: list[str] = Field(default_factory=list)


class FinancialYearSchema(BaseModel):
    financial_year: str

class EmployeeDetailSchema(BaseModel):
    user_id: str
    name: str
    pan: str
    office_school_name: Optional[str] = None
    tan_id: Optional[str] = None
    email: Optional[str] = None
    mobile: Optional[str] = None
    visitor_session_id: Optional[str] = None
    email_contact_consent: bool = False


class AdminSettingsSchema(BaseModel):
    fee_amount: float = Field(..., ge=0)
    upi_id: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

# =========================================================
# HTML PAGES
# =========================================================

@app.get("/")
def read_root(request: Request):

    return templates.TemplateResponse(
        request=request,
        name="upload.html",
    )


@app.get("/privacy")
def privacy_page(request: Request):
    return templates.TemplateResponse(request=request, name="privacy.html")


@app.get("/terms")
def terms_page(request: Request):
    return templates.TemplateResponse(request=request, name="terms.html")


@app.get("/contact")
def contact_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="contact.html",
        context={"support_email": os.getenv("SUPPORT_EMAIL", os.getenv("ADMIN_EMAIL", ""))},
    )


@app.get("/refund")
def refund_page(request: Request):
    return templates.TemplateResponse(
        request=request, name="refund.html",
        context={"support_email": os.getenv("SUPPORT_EMAIL", os.getenv("ADMIN_EMAIL", ""))},
    )


@app.get("/review")
def review_page(
    request: Request,
    user_id: str = "",
):

    return templates.TemplateResponse(
        request=request,
        name="review.html",
        context={
            "user_id": user_id,
        },
    )


@app.get("/ddo-details")
def ddo_details_page(
    request: Request,
    user_id: str = "",
):

    return templates.TemplateResponse(
        request=request,
        name="ddo_details.html",
        context={
            "user_id": user_id,
        },
    )


@app.get("/payment")
def payment_page(
    request: Request,
    user_id: str = "",
    db: Session = Depends(get_db),
):

    settings = db.query(AdminSettings).first()

    upi_id = (
        settings.upi_id
        if settings and settings.upi_id
        else ""
    )

    fee_amount = (
        settings.fee_amount
        if settings and settings.fee_amount is not None
        else 150.0
    )

    financial_year = (
        settings.financial_year
        if settings
        else None
    )

    return templates.TemplateResponse(
        request=request,
        name="payment.html",
        context={
            "upi_id": upi_id,
            "fee_amount": fee_amount,
            "user_id": user_id,
            "financial_year": financial_year,
        },
    )

# =========================================================
# EMPLOYEE DETAILS
# =========================================================

@app.post("/api/employee")
def save_employee_details(
    data: EmployeeDetailSchema,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = data.user_id.strip()
    require_journey_user(request, db, user_id)
    name = data.name.strip()
    pan = data.pan.strip().upper()

    try:
        email = normalise_email(data.email)
        mobile = normalise_mobile(data.mobile)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    tan_id = (
        data.tan_id.strip().upper()
        if data.tan_id
        else None
    )

    office_school_name = (
        data.office_school_name.strip()
        if data.office_school_name
        else None
    )

    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="User ID is required.",
        )

    if not name:
        raise HTTPException(
            status_code=400,
            detail="Employee name is required.",
        )

    if not pan:
        raise HTTPException(
            status_code=400,
            detail="Employee PAN is required.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user:
        user = User(id=user_id)
        db.add(user)
        db.flush()

    if email:
        duplicate_email = (
            db.query(User)
            .filter(
                User.email == email,
                User.id != user_id,
            )
            .first()
        )
        if duplicate_email:
            raise HTTPException(
                status_code=400,
                detail="This email is already linked to another user.",
            )
        user.email = email

    if mobile:
        duplicate_mobile = (
            db.query(User)
            .filter(
                User.mobile == mobile,
                User.id != user_id,
            )
            .first()
        )
        if duplicate_mobile:
            raise HTTPException(
                status_code=400,
                detail="This mobile number is already linked to another user.",
            )
        user.mobile = mobile

    if tan_id:
        employer = (
            db.query(EmployerCache)
            .filter(EmployerCache.tan == tan_id)
            .first()
        )

        if not employer:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Employer TAN must be saved "
                    "before linking employee details."
                ),
            )

    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if employee:
        employee.name = name
        employee.pan = pan
        employee.office_school_name = office_school_name

        if tan_id:
            employee.tan_id = tan_id

    else:
        employee = EmployeeDetail(
            id=str(uuid.uuid4()),
            user_id=user_id,
            name=name,
            pan=pan,
            office_school_name=office_school_name,
            tan_id=tan_id,
        )
        db.add(employee)

    visitor_session = find_active_visitor_session(
        db,
        journey_id=data.visitor_session_id,
        user_id=user_id,
    )

    if visitor_session:
        visitor_session.user_id = user_id

        if email:
            visitor_session.email = email

        if mobile:
            visitor_session.mobile = mobile

        visitor_session.current_page = "review"
        visitor_session.current_step = "contact_saved"
        visitor_session.last_completed_step = "review"
        visitor_session.progress_percent = max(
            int(visitor_session.progress_percent or 0),
            45,
        )
        visitor_session.last_seen_at = datetime.utcnow()

        snapshot = dict(visitor_session.form_snapshot_json or {})
        snapshot.update(
            safe_snapshot(
                {
                    "name": name,
                    "pan": pan,
                    "office_school_name": office_school_name,
                    "tan_id": tan_id,
                }
            )
        )
        visitor_session.form_snapshot_json = snapshot

        if email and data.email_contact_consent:
            visitor_session.email_contact_consent = True
            visitor_session.reminder_status = "pending"
            visitor_session.reminder_due_at = (
                datetime.utcnow()
                + timedelta(hours=REMINDER_DELAY_HOURS)
            )

        add_visitor_event(
            db,
            visitor_session,
            "contact_saved",
            page_name="review",
            step_name="contact_saved",
            event_data={
                "has_email": bool(email),
                "has_mobile": bool(mobile),
            },
        )

    try:
        db.commit()
        db.refresh(employee)

    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Employee details could not be saved.",
        )

    return {
        "message": "Employee details saved successfully.",
        "user_id": employee.user_id,
        "name": employee.name,
        "pan": employee.pan,
        "office_school_name": employee.office_school_name,
        "tan_id": employee.tan_id,
        "email": user.email,
        "mobile": user.mobile,
    }


@app.get("/api/employee/{user_id}")
def get_employee_details(
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    require_journey_user(request, db, user_id)
    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee details not found.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    return {
        "user_id": employee.user_id,
        "name": employee.name,
        "pan": employee.pan,
        "office_school_name": employee.office_school_name,
        "tan_id": employee.tan_id,
        "email": user.email if user else None,
        "mobile": user.mobile if user else None,
    }


# =========================================================
# VISITOR / APPLICATION PROGRESS API
# =========================================================

class ProgressSchema(BaseModel):
    user_id: Optional[str] = None
    page_name: str
    step_name: str
    last_completed_step: Optional[str] = None
    progress_percent: int = Field(default=0, ge=0, le=100)
    email: Optional[str] = None
    mobile: Optional[str] = None
    email_contact_consent: bool = False
    snapshot: Dict[str, Any] = Field(default_factory=dict)


@app.post("/api/progress")
def save_application_progress(
    data: ProgressSchema,
    request: Request,
    db: Session = Depends(get_db),
):
    journey_id = request.cookies.get("form16_journey_id")
    visitor_id = request.cookies.get("form16_visitor_id")

    if data.user_id:
        require_journey_user(request, db, data.user_id)

    session = find_active_visitor_session(
        db,
        journey_id=journey_id,
        user_id=data.user_id,
    )

    if not session:
        session, _, _ = get_or_create_visitor_session(
            db,
            visitor_id,
            journey_id,
        )

    if data.user_id:
        session.user_id = data.user_id

    try:
        email = normalise_email(data.email)
        mobile = normalise_mobile(data.mobile)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    if email:
        session.email = email

    if mobile:
        session.mobile = mobile

    session.current_page = data.page_name.strip()[:100]
    session.current_step = data.step_name.strip()[:100]

    if data.last_completed_step:
        session.last_completed_step = (
            data.last_completed_step.strip()[:100]
        )

    session.progress_percent = max(
        int(session.progress_percent or 0),
        data.progress_percent,
    )
    session.last_seen_at = datetime.utcnow()

    snapshot = dict(session.form_snapshot_json or {})
    snapshot.update(safe_snapshot(data.snapshot))
    session.form_snapshot_json = snapshot

    if email and data.email_contact_consent:
        session.email_contact_consent = True

        if session.payment_status != "approved":
            session.reminder_status = "pending"
            session.reminder_due_at = (
                datetime.utcnow()
                + timedelta(hours=REMINDER_DELAY_HOURS)
            )

    add_visitor_event(
        db,
        session,
        "step_completed"
        if data.last_completed_step
        else "progress_saved",
        page_name=session.current_page,
        step_name=session.current_step,
    )

    db.commit()

    return {
        "message": "Progress saved.",
        "session_id": session.id,
        "resume_token": session.resume_token,
        "progress_percent": session.progress_percent,
    }


@app.get("/resume/{resume_token}")
def resume_application(
    resume_token: str,
    db: Session = Depends(get_db),
):
    session = (
        db.query(VisitorSession)
        .filter(VisitorSession.resume_token == resume_token)
        .first()
    )

    if not session:
        raise HTTPException(
            status_code=404,
            detail="Resume link is invalid or expired.",
        )

    page_routes = {
        "upload": "/",
        "review": "/review",
        "ddo_details": "/ddo-details",
        "payment": "/payment",
        "payment_wait": "/payment",
        "completed": "/",
    }

    target = page_routes.get(
        session.current_page or "",
        "/",
    )

    if session.user_id and target != "/":
        separator = "&" if "?" in target else "?"
        target = (
            f"{target}{separator}user_id={session.user_id}"
        )

    response = RedirectResponse(
        url=target,
        status_code=303,
    )

    response.set_cookie(
        "form16_visitor_id",
        session.visitor_id,
        max_age=365 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        "form16_journey_id",
        session.id,
        max_age=30 * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
    )

    return response


@app.post("/api/internal/process-reminders")
def process_reminders_endpoint(
    request: Request,
):
    expected = os.getenv("REMINDER_CRON_SECRET", "").strip()
    supplied = request.headers.get("x-reminder-secret", "").strip()

    if not expected or not hmac.compare_digest(expected, supplied):
        raise HTTPException(
            status_code=401,
            detail="Reminder processor authentication failed.",
        )

    return process_due_reminders()


# =========================================================
# EMPLOYER / TAN CACHE
# =========================================================

@app.get(
    "/api/employer/{tan}",
    response_model=EmployerSchema,
)
def get_employer_by_tan(
    tan: str,
    user_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    require_journey_user(request, db, user_id.strip())
    tan = tan.strip().upper()

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == tan)
        .first()
    )

    if not employer or (not bool(getattr(employer, "is_verified", False)) and getattr(employer, "created_by_user_id", None) != user_id.strip()):
        raise HTTPException(
            status_code=404,
            detail=(
                "TAN not found in the verified cache. "
                "Employer details must be entered manually."
            ),
        )

    return employer


@app.post(
    "/api/employer",
    response_model=EmployerSchema,
)
def upsert_employer(
    emp_data: EmployerSchema,
    request: Request,
    db: Session = Depends(get_db),
):
    user_id = (emp_data.user_id or "").strip()
    require_journey_user(request, db, user_id)
    tan = emp_data.tan.strip().upper()

    employer = (
        db.query(EmployerCache)
        .filter(EmployerCache.tan == tan)
        .first()
    )

    if employer:
        # SECURITY: verified shared TAN records are immutable to customers.
        # The creator may correct their own still-private/unverified draft.
        if bool(getattr(employer, "is_verified", False)):
            return employer
        if getattr(employer, "created_by_user_id", None) != user_id:
            raise HTTPException(status_code=409, detail="This TAN is awaiting administrator verification.")
        employer.officer_name = emp_data.officer_name
        employer.officer_father_name = emp_data.officer_father_name
        employer.employer_name = emp_data.employer_name
        employer.employer_address = emp_data.employer_address
        employer.designation = emp_data.designation
        employer.pan = emp_data.pan
        employer.cit_tds_address = emp_data.cit_tds_address
        employer.cit_tds_city = emp_data.cit_tds_city
        employer.cit_tds_pincode = emp_data.cit_tds_pincode
    else:

        employer = EmployerCache(
            tan=tan,
            officer_name=emp_data.officer_name,
            officer_father_name=(
                emp_data.officer_father_name
            ),
            employer_name=emp_data.employer_name,
            employer_address=(
                emp_data.employer_address
            ),
            designation=emp_data.designation,
            pan=emp_data.pan,
            cit_tds_address=emp_data.cit_tds_address,
            cit_tds_city=emp_data.cit_tds_city,
            cit_tds_pincode=emp_data.cit_tds_pincode,
            is_verified=False,
            created_by_user_id=user_id,
        )

        db.add(employer)

    db.commit()
    db.refresh(employer)

    return employer


@app.post("/api/admin/employer/{tan}/verify")
def admin_verify_employer(tan: str, admin_session: str = Cookie(None), db: Session = Depends(get_db)):
    require_admin_session(admin_session)
    row = db.query(EmployerCache).filter(EmployerCache.tan == tan.strip().upper()).first()
    if not row:
        raise HTTPException(status_code=404, detail="Employer TAN not found.")
    row.is_verified = True
    db.commit()
    return {"ok": True, "tan": row.tan, "is_verified": True}


# =========================================================
# SALARY-SLIP EXTRACTION
# =========================================================

@app.post("/api/extract")
async def extract_salary_slip(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):

    filename = file.filename or ""

    if not filename.lower().endswith(".pdf"):
        raise HTTPException(
            status_code=400,
            detail="Only PDF files are allowed.",
        )

    tmp_path = None

    try:

        upload_bytes = await file.read(15 * 1024 * 1024 + 1)
        if len(upload_bytes) > 15 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF exceeds the 15 MB upload limit.")
        if not upload_bytes.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF.")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(upload_bytes)
            tmp_path = tmp.name

        parsed_result = load_and_parse_pdf(tmp_path)

        if not parsed_result.get("success"):

            raise HTTPException(
                status_code=400,
                detail=parsed_result.get(
                    "error",
                    "Failed to parse PDF.",
                ),
            )

        user_id = bind_new_user_to_journey(request, db)
        return {
            "message": "Extracted successfully",
            "data": parsed_result.get("slips", []),
            "user_id": user_id,
        }

    except HTTPException:
        raise

    except Exception as exc:

        raise HTTPException(
            status_code=500,
            detail="The PDF could not be processed safely. Please verify the file and try again.",
        )

    finally:

        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)


# =========================================================
# MONTHLY LEDGER
# =========================================================

def _numeric_amount_map(values: Optional[dict]) -> dict:
    """Keep only finite numeric values from a client supplied amount map."""
    result = {}

    for raw_key, raw_value in (values or {}).items():
        key = re.sub(r"[^a-z0-9_]+", "_", str(raw_key).strip().lower())
        key = key.strip("_")
        if not key:
            continue

        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        # NaN is the only normal float that is not equal to itself.
        if value != value:
            continue

        result[key] = round(value, 2)

    return result


def _merge_amount_maps(base: Optional[dict], extra: Optional[dict]) -> dict:
    """Merge numeric amount maps, adding duplicate keys instead of losing money."""
    result = _numeric_amount_map(base)

    for key, value in _numeric_amount_map(extra).items():
        result[key] = round(float(result.get(key, 0)) + value, 2)

    return result


def _merge_unique_flags(*flag_groups) -> list[str]:
    result = []
    seen = set()

    for group in flag_groups:
        for raw_flag in (group or []):
            flag = str(raw_flag).strip()
            if not flag or flag in seen:
                continue
            seen.add(flag)
            result.append(flag)

    return result


def _append_ledger_note(existing_note: Optional[str], new_note: Optional[str]) -> Optional[str]:
    parts = []

    for raw in (existing_note, new_note):
        value = (raw or "").strip()
        if value and value not in parts:
            parts.append(value)

    return " | ".join(parts) if parts else None


def _arrear_prefixed_amounts(values: Optional[dict]) -> dict:
    """
    Keep arrear components separate from regular monthly Basic/DA/HRA.
    Example: basic_pay -> arrear_basic_pay.
    """
    result = {}

    for key, value in _numeric_amount_map(values).items():
        target_key = key if key.startswith("arrear_") else f"arrear_{key}"
        result[target_key] = value

    return result


def _arrear_gross_from_flags(flags: Optional[list]) -> float:
    """Recover arrear gross already merged into a month during safe replacement."""
    total = 0.0

    for raw_flag in (flags or []):
        flag = str(raw_flag)
        if not flag.startswith("arrear_gross:"):
            continue

        # Format: arrear_gross:<amount>:<fingerprint>
        parts = flag.split(":", 2)
        if len(parts) < 2:
            continue

        try:
            total += float(parts[1])
        except (TypeError, ValueError):
            continue

    return round(total, 2)


def _arrear_only_amounts(values: Optional[dict]) -> dict:
    return {
        key: value
        for key, value in _numeric_amount_map(values).items()
        if key.startswith("arrear_")
    }


def _arrear_only_flags(flags: Optional[list]) -> list[str]:
    prefixes = (
        "contains_arrear",
        "arrear_fingerprint:",
        "arrear_gross:",
        "arrear_original_period:",
        "arrear_payment_month:",
        "arrear_bill_no:",
    )

    return [
        str(flag)
        for flag in (flags or [])
        if str(flag).startswith(prefixes)
    ]


def _arrear_fingerprint(
    user_id: str,
    original_period: str,
    payment_month: int,
    payment_year: int,
    gross_salary: float,
    bill_no: Optional[str],
) -> str:
    raw = "|".join(
        [
            user_id.strip(),
            original_period.strip().lower(),
            f"{payment_year:04d}-{payment_month:02d}",
            f"{gross_salary:.2f}",
            (bill_no or "").strip().lower(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


@app.post(
    "/api/ledger/save",
    status_code=status.HTTP_201_CREATED,
)
def save_monthly_ledger(
    ledger_data: LedgerSchema,
    request: Request,
    db: Session = Depends(get_db),
):

    user_id = ledger_data.user_id.strip()
    require_journey_user(request, db, user_id)

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user:
        raise HTTPException(
            status_code=404,
            detail="User not found.",
        )

    source = (ledger_data.source or "extracted").strip().lower()

    if source == "arrear":
        raise HTTPException(
            status_code=400,
            detail=(
                "Arrear must be saved through /api/ledger/save-arrear "
                "with its actual payment/disbursement month and year."
            ),
        )

    financial_year = get_active_financial_year(db)

    if not month_belongs_to_financial_year(
        ledger_data.month,
        ledger_data.year,
        financial_year,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{ledger_data.month}/{ledger_data.year} "
                f"does not belong to FY {financial_year}."
            ),
        )

    month_year = month_year_string(
        ledger_data.year,
        ledger_data.month,
    )

    incoming_line_items = _numeric_amount_map(ledger_data.line_items)
    incoming_deductions = _numeric_amount_map(ledger_data.deductions)
    incoming_flags = _merge_unique_flags(ledger_data.flags)

    existing = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id == user_id,
            MonthlyLedger.month_year == month_year,
        )
        .first()
    )

    if existing:
        existing_flags = list(existing.flags or [])
        arrear_gross = _arrear_gross_from_flags(existing_flags)
        arrear_line_items = _arrear_only_amounts(existing.line_items_json)
        arrear_deductions = _arrear_only_amounts(existing.deductions_json)
        arrear_flags = _arrear_only_flags(existing_flags)
        has_arrear = arrear_gross > 0 or bool(arrear_flags)

        # Actual salary slip replaces an auto-generated Jan/Feb projection,
        # but any arrear already received in that payment month is preserved.
        if (
            existing.is_auto_generated
            and source != "auto_generated"
        ):
            existing.month = ledger_data.month
            existing.year = ledger_data.year
            existing.financial_year = financial_year

            existing.basic_pay = float(ledger_data.basic_pay or 0)
            existing.da = float(ledger_data.da or 0)
            existing.hra = float(ledger_data.hra or 0)
            existing.gross_salary = round(
                float(ledger_data.gross_salary or 0) + arrear_gross,
                2,
            )

            existing.line_items_json = {
                **incoming_line_items,
                **arrear_line_items,
            }
            existing.deductions_json = {
                **incoming_deductions,
                **arrear_deductions,
            }

            existing.source = source
            existing.is_auto_generated = False
            existing.note = _append_ledger_note(
                existing.note if has_arrear else None,
                ledger_data.note,
            )
            existing.flags = _merge_unique_flags(
                incoming_flags,
                arrear_flags,
            )

            db.commit()
            db.refresh(existing)

            return {
                "message": (
                    "Actual salary slip replaced the auto-generated projection"
                    + (" and preserved arrear." if has_arrear else ".")
                ),
                "ledger_id": existing.id,
                "month_year": month_year,
                "financial_year": financial_year,
                "contains_arrear": has_arrear,
            }

        # If an arrear was stored before the ordinary salary slip for the same
        # payment month, merge the regular salary into that row instead of
        # rejecting it as a duplicate. This avoids losing either amount.
        if existing.source == "arrear" and source != "auto_generated":
            existing.basic_pay = float(ledger_data.basic_pay or 0)
            existing.da = float(ledger_data.da or 0)
            existing.hra = float(ledger_data.hra or 0)
            existing.gross_salary = round(
                float(ledger_data.gross_salary or 0) + arrear_gross,
                2,
            )
            existing.line_items_json = {
                **incoming_line_items,
                **arrear_line_items,
            }
            existing.deductions_json = {
                **incoming_deductions,
                **arrear_deductions,
            }
            existing.source = source
            existing.is_auto_generated = False
            existing.note = _append_ledger_note(
                existing.note,
                ledger_data.note,
            )
            existing.flags = _merge_unique_flags(
                incoming_flags,
                arrear_flags,
            )

            db.commit()
            db.refresh(existing)

            return {
                "message": "Regular salary merged with the arrear payment month.",
                "ledger_id": existing.id,
                "month_year": month_year,
                "financial_year": financial_year,
                "contains_arrear": True,
            }

        raise HTTPException(
            status_code=400,
            detail=(
                f"Ledger entry for {month_year} already exists."
            ),
        )

    new_ledger = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=user_id,
        month=ledger_data.month,
        year=ledger_data.year,
        month_year=month_year,
        financial_year=financial_year,
        basic_pay=float(ledger_data.basic_pay or 0),
        da=float(ledger_data.da or 0),
        hra=float(ledger_data.hra or 0),
        gross_salary=float(ledger_data.gross_salary or 0),
        source=source,
        is_auto_generated=(source == "auto_generated"),
        line_items_json=incoming_line_items,
        deductions_json=incoming_deductions,
        note=ledger_data.note,
        flags=incoming_flags,
    )

    db.add(new_ledger)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail=f"Ledger entry for {month_year} already exists.",
        )

    db.refresh(new_ledger)

    return {
        "message": "Salary data stored successfully",
        "ledger_id": new_ledger.id,
        "month_year": month_year,
        "financial_year": financial_year,
        "contains_arrear": False,
    }


@app.post(
    "/api/ledger/save-arrear",
    status_code=status.HTTP_201_CREATED,
)
def save_arrear_ledger(
    arrear_data: ArrearLedgerSchema,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    Store arrear by the month in which it was ACTUALLY paid/disbursed.

    The old salary period is preserved as metadata only. It is never used to
    decide the Form-16 FY, because doing so can silently move arrear income to
    the wrong tax cycle.
    """
    user_id = arrear_data.user_id.strip()
    require_journey_user(request, db, user_id)
    original_period = arrear_data.original_period.strip()
    bill_no = (arrear_data.bill_no or "").strip() or None

    if not user_id:
        raise HTTPException(status_code=400, detail="User ID is required.")

    if not original_period:
        raise HTTPException(
            status_code=400,
            detail="Original arrear period is required.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user:
        raise HTTPException(status_code=404, detail="User not found.")

    financial_year = get_active_financial_year(db)

    if not month_belongs_to_financial_year(
        arrear_data.payment_month,
        arrear_data.payment_year,
        financial_year,
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "The arrear payment/disbursement month "
                f"{arrear_data.payment_month}/{arrear_data.payment_year} "
                f"does not belong to the selected FY {financial_year}. "
                "Select the FY in which the arrear was actually paid."
            ),
        )

    line_items = _numeric_amount_map(arrear_data.line_items)
    deductions = _numeric_amount_map(arrear_data.deductions)

    gross_salary = round(float(arrear_data.gross_salary or 0), 2)
    component_total = round(sum(line_items.values()), 2)

    if gross_salary <= 0 and component_total > 0:
        gross_salary = component_total

    if gross_salary <= 0:
        raise HTTPException(
            status_code=400,
            detail="Arrear gross amount must be greater than zero.",
        )

    payment_month_year = month_year_string(
        arrear_data.payment_year,
        arrear_data.payment_month,
    )

    fingerprint = _arrear_fingerprint(
        user_id=user_id,
        original_period=original_period,
        payment_month=arrear_data.payment_month,
        payment_year=arrear_data.payment_year,
        gross_salary=gross_salary,
        bill_no=bill_no,
    )

    fingerprint_flag = f"arrear_fingerprint:{fingerprint}"
    gross_flag = f"arrear_gross:{gross_salary:.2f}:{fingerprint}"

    metadata_flags = _merge_unique_flags(
        arrear_data.flags,
        [
            "contains_arrear",
            fingerprint_flag,
            gross_flag,
            f"arrear_original_period:{original_period}",
            f"arrear_payment_month:{payment_month_year}",
        ],
        [f"arrear_bill_no:{bill_no}"] if bill_no else [],
    )

    arrear_note = (
        f"Arrear for {original_period}; actually paid/disbursed in "
        f"{payment_month_year}; included in FY {financial_year}."
    )
    arrear_note = _append_ledger_note(arrear_note, arrear_data.note)

    prefixed_line_items = _arrear_prefixed_amounts(line_items)
    prefixed_deductions = _arrear_prefixed_amounts(deductions)

    existing = (
        db.query(MonthlyLedger)
        .filter(
            MonthlyLedger.user_id == user_id,
            MonthlyLedger.month_year == payment_month_year,
        )
        .first()
    )

    if existing:
        existing_flags = list(existing.flags or [])

        # Safe retry: never add the same arrear twice after a network refresh.
        if fingerprint_flag in existing_flags:
            return {
                "message": "This arrear is already stored.",
                "ledger_id": existing.id,
                "month_year": payment_month_year,
                "financial_year": financial_year,
                "arrear_fingerprint": fingerprint,
                "already_saved": True,
            }

        existing.gross_salary = round(
            float(existing.gross_salary or 0) + gross_salary,
            2,
        )
        existing.line_items_json = _merge_amount_maps(
            existing.line_items_json,
            prefixed_line_items,
        )
        existing.deductions_json = _merge_amount_maps(
            existing.deductions_json,
            prefixed_deductions,
        )
        existing.note = _append_ledger_note(existing.note, arrear_note)
        existing.flags = _merge_unique_flags(existing_flags, metadata_flags)

        # Keep a real/auto regular salary source if one already exists. If the
        # row contains only arrear, mark it explicitly so automation won't use
        # it as a clean ordinary salary month.
        if not existing.source:
            existing.source = "arrear"

        db.commit()
        db.refresh(existing)

        return {
            "message": "Arrear merged into its actual payment month.",
            "ledger_id": existing.id,
            "month_year": payment_month_year,
            "financial_year": financial_year,
            "arrear_fingerprint": fingerprint,
            "already_saved": False,
        }

    new_ledger = MonthlyLedger(
        id=str(uuid.uuid4()),
        user_id=user_id,
        month=arrear_data.payment_month,
        year=arrear_data.payment_year,
        month_year=payment_month_year,
        financial_year=financial_year,

        # Do not pretend an old-period arrear is the ordinary Basic/DA/HRA of
        # the payment month. The components stay in arrear_* line items.
        basic_pay=0,
        da=0,
        hra=0,
        gross_salary=gross_salary,
        source="arrear",
        is_auto_generated=False,
        line_items_json=prefixed_line_items,
        deductions_json=prefixed_deductions,
        note=arrear_note,
        flags=metadata_flags,
    )

    db.add(new_ledger)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=(
                "The payment-month ledger changed while the arrear was being "
                "saved. Please retry once; the endpoint is idempotent."
            ),
        )

    db.refresh(new_ledger)

    return {
        "message": "Arrear stored against its actual payment month.",
        "ledger_id": new_ledger.id,
        "month_year": payment_month_year,
        "financial_year": financial_year,
        "arrear_fingerprint": fingerprint,
        "already_saved": False,
    }


# =========================================================
# ADMIN LOGIN
# =========================================================

@app.get(
    "/admin",
    response_class=HTMLResponse,
)
async def admin_page(
    request: Request,
    admin_session: str = Cookie(None),
):
    if is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin/dashboard",
            status_code=303,
        )

    csrf = request.cookies.get("csrf_token") or _new_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="admin_login.html",
        context={"request": request, "error": None, "csrf_token": csrf},
    )
    response.set_cookie("csrf_token", csrf, max_age=ADMIN_SESSION_MAX_AGE, httponly=False, samesite="strict", secure=request_uses_https(request))
    return response


@app.post("/admin/login")
async def admin_login(request: Request, username: str = Form(...), password: str = Form(...), csrf_token: str = Form(...)):
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(403, "CSRF validation failed.")
    if _login_is_locked(request):
        return templates.TemplateResponse(request=request, name="admin_login.html", context={"request": request, "error": "Too many failed attempts. Try again in 15 minutes.", "csrf_token": csrf_token}, status_code=429)
    if not _admin_username() or not _admin_password():
        raise HTTPException(503, "Admin credentials are not configured.")
    if not (hmac.compare_digest(username, _admin_username()) and hmac.compare_digest(password, _admin_password())):
        _record_login_failure(request)
        time.sleep(0.35)
        return templates.TemplateResponse(request=request, name="admin_login.html", context={"request": request, "error": "Invalid credentials.", "csrf_token": csrf_token}, status_code=401)
    _clear_login_failures(request)
    admin_email = os.getenv("ADMIN_EMAIL", "").strip()
    if not admin_email:
        raise HTTPException(503, "ADMIN_EMAIL is required for admin OTP login.")
    cid = str(uuid.uuid4()); otp = f"{secrets.randbelow(1000000):06d}"; now=datetime.utcnow()
    db=SessionLocal()
    try:
        db.add(AdminLoginChallenge(id=cid, otp_hash=_otp_hash(cid, otp), expires_at=now+timedelta(minutes=10), ip_hash=_request_ip_hash(request)))
        db.commit()
    finally: db.close()
    send_email_message(admin_email, "VetanKosh admin login OTP", f"Your VetanKosh admin OTP is {otp}. It expires in 10 minutes. If this was not you, change your admin password immediately.")
    response=RedirectResponse("/admin/otp", status_code=303)
    response.set_cookie("admin_challenge", cid, max_age=600, httponly=True, samesite="strict", secure=request_uses_https(request))
    return response

@app.get("/admin/otp", response_class=HTMLResponse)
async def admin_otp_page(request: Request):
    csrf = request.cookies.get("csrf_token") or _new_csrf_token()
    response = templates.TemplateResponse(request=request, name="admin_otp.html", context={"request":request,"error":None,"csrf_token":csrf})
    response.set_cookie("csrf_token", csrf, max_age=600, httponly=False, samesite="strict", secure=request_uses_https(request))
    return response

@app.post("/admin/otp")
async def admin_otp_verify(request: Request, otp: str = Form(...), csrf_token: str = Form(...), admin_challenge: str = Cookie(None)):
    if not _csrf_ok(request, csrf_token):
        raise HTTPException(403, "CSRF validation failed.")
    db=SessionLocal(); now=datetime.utcnow()
    try:
        row=db.query(AdminLoginChallenge).filter(AdminLoginChallenge.id==admin_challenge).first() if admin_challenge else None
        if not row or row.consumed_at or row.expires_at <= now or row.attempts >= 5:
            return templates.TemplateResponse(request=request,name="admin_otp.html",context={"request":request,"error":"OTP expired or invalid.","csrf_token":csrf_token},status_code=401)
        row.attempts += 1
        if not hmac.compare_digest(row.otp_hash, _otp_hash(row.id, otp.strip())):
            db.commit(); return templates.TemplateResponse(request=request,name="admin_otp.html",context={"request":request,"error":"Invalid OTP.","csrf_token":csrf_token},status_code=401)
        row.consumed_at=now; db.commit()
    finally: db.close()
    token=create_admin_session_token(request)
    response=RedirectResponse("/admin/dashboard",status_code=303)
    response.set_cookie("admin_session",token,max_age=ADMIN_SESSION_MAX_AGE,httponly=True,samesite="strict",secure=request_uses_https(request))
    if not request.cookies.get("csrf_token"):
        response.set_cookie("csrf_token", _new_csrf_token(), max_age=ADMIN_SESSION_MAX_AGE, httponly=False, samesite="strict", secure=request_uses_https(request))
    response.delete_cookie("admin_challenge")
    try:
        send_email_message(os.getenv("ADMIN_EMAIL",""), "VetanKosh admin login alert", f"A VetanKosh admin login succeeded at {datetime.utcnow().isoformat()} UTC. Browser: {request.headers.get('user-agent','')[:180]}")
    except Exception: pass
    return response

@app.get("/admin/logout")
async def admin_logout(admin_session: str = Cookie(None)):
    if admin_session:
        db=SessionLocal()
        try:
            row=db.query(AdminSession).filter(AdminSession.token_hash==_token_hash(admin_session)).first()
            if row: row.revoked_at=datetime.utcnow(); db.commit()
        finally: db.close()
    response = RedirectResponse(
        url="/admin",
        status_code=303,
    )
    response.delete_cookie("admin_session")
    return response


# =========================================================
# ADMIN SETTINGS
# =========================================================

@app.post("/api/admin/financial-year")
def set_financial_year(
    data: FinancialYearSchema,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    try:
        financial_year = validate_financial_year(
            data.financial_year
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        )

    settings = db.query(AdminSettings).first()

    if not settings:
        settings = AdminSettings(
            financial_year=financial_year,
        )
        db.add(settings)
    else:
        settings.financial_year = financial_year

    db.commit()

    return {
        "message": "Financial year updated.",
        "financial_year": financial_year,
        "assessment_year": (
            f"{int(financial_year[:4]) + 1}-"
            f"{(int(financial_year[:4]) + 2) % 100:02d}"
        ),
    }


@app.post("/api/admin/settings")
def update_admin_settings(
    data: AdminSettingsSchema,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    settings = db.query(AdminSettings).first()
    if not settings:
        settings = AdminSettings()
        db.add(settings)

    settings.fee_amount = float(data.fee_amount)
    settings.upi_id = data.upi_id.strip()
    settings.telegram_bot_token = (
        data.telegram_bot_token.strip()
    )
    settings.telegram_chat_id = (
        data.telegram_chat_id.strip()
    )

    db.commit()
    db.refresh(settings)

    return {
        "message": "Admin settings updated.",
        "fee_amount": settings.fee_amount,
        "upi_id": settings.upi_id or "",
        "telegram_bot_token": (
            settings.telegram_bot_token or ""
        ),
        "telegram_chat_id": (
            settings.telegram_chat_id or ""
        ),
    }


# =========================================================
# ADMIN DASHBOARD
# =========================================================

@app.get(
    "/admin/dashboard",
    response_class=HTMLResponse,
)
async def admin_dashboard(
    request: Request,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payments = (
        db.query(Payment)
        .order_by(Payment.created_at.desc())
        .all()
    )

    dashboard_data = []

    for payment in payments:
        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == payment.user_id)
            .first()
        )
        user = (
            db.query(User)
            .filter(User.id == payment.user_id)
            .first()
        )

        dashboard_data.append(
            {
                "payment_id": payment.id,
                "id": payment.id,
                "user_id": payment.user_id,
                "name": employee.name if employee else "Unknown Employee",
                "pan": employee.pan if employee else "N/A",
                "email": user.email if user else None,
                "mobile": user.mobile if user else None,
                "utr_number": payment.upi_txn_utr,
                "amount": payment.amount or 0,
                "status": payment.status,
                "created_at": payment.created_at,
                "approved_at": payment.approved_at,
                "is_admin_generated": False,
            }
        )

    settings = db.query(AdminSettings).first()

    financial_year = (
        settings.financial_year
        if settings
        else None
    )

    assessment_year = None
    if financial_year:
        try:
            assessment_year = assessment_year_from_financial_year(
                financial_year
            )
        except ValueError:
            assessment_year = None

    pending_count = sum(
        1 for item in dashboard_data
        if item["status"] == "pending"
    )
    approved_count = sum(
        1 for item in dashboard_data
        if item["status"] == "approved"
    )
    rejected_count = sum(
        1 for item in dashboard_data
        if item["status"] == "rejected"
    )
    total_revenue = sum(
        float(item["amount"] or 0)
        for item in dashboard_data
        if item["status"] == "approved"
    )

    employees = (
        db.query(EmployeeDetail)
        .order_by(EmployeeDetail.name.asc())
        .all()
    )

    visitor_sessions = (
        db.query(VisitorSession)
        .order_by(VisitorSession.last_seen_at.desc())
        .all()
    )

    incomplete_users = []
    for session in visitor_sessions:
        if session.application_status == "completed":
            continue

        if session.payment_status == "approved":
            continue

        employee = None
        if session.user_id:
            employee = (
                db.query(EmployeeDetail)
                .filter(EmployeeDetail.user_id == session.user_id)
                .first()
            )

        incomplete_users.append(
            {
                "session_id": session.id,
                "user_id": session.user_id,
                "name": (
                    employee.name
                    if employee
                    else (session.form_snapshot_json or {}).get("name")
                ),
                "email": session.email,
                "mobile": session.mobile,
                "current_page": session.current_page,
                "current_step": session.current_step,
                "last_completed_step": session.last_completed_step,
                "progress_percent": session.progress_percent or 0,
                "application_status": session.application_status,
                "payment_status": session.payment_status,
                "reminder_status": session.reminder_status,
                "last_seen_at": session.last_seen_at,
            }
        )

    generations = (
        db.query(Form16Generation)
        .order_by(Form16Generation.created_at.desc())
        .all()
    )

    admin_generated = []
    for generation in generations:
        if generation.source != "admin":
            continue

        employee = (
            db.query(EmployeeDetail)
            .filter(EmployeeDetail.user_id == generation.user_id)
            .first()
        )
        user = (
            db.query(User)
            .filter(User.id == generation.user_id)
            .first()
        )

        admin_generated.append(
            {
                "id": generation.id,
                "generation_id": generation.id,
                "user_id": generation.user_id,
                "name": employee.name if employee else "Unknown Employee",
                "pan": employee.pan if employee else "N/A",
                "email": user.email if user else None,
                "financial_year": generation.financial_year,
                "status": generation.status,
                "created_at": generation.created_at,
                "generated_at": generation.generated_at,
                "download_count": generation.download_count or 0,
            }
        )

    unique_visitors = (
        db.query(VisitorSession.visitor_id)
        .distinct()
        .count()
    )

    form_started = db.query(VisitorSession).count()

    payment_page_count = (
        db.query(VisitorSession)
        .filter(
            VisitorSession.progress_percent >= 80
        )
        .count()
    )

    email_sent_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "sent")
        .count()
    )

    email_failed_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "failed")
        .count()
    )

    csrf_token = request.cookies.get("csrf_token") or _new_csrf_token()
    response = templates.TemplateResponse(
        request=request,
        name="admin_dashboard.html",
        context={
            "request": request,
            "users": dashboard_data,
            "payments": dashboard_data,
            "admin_generated": admin_generated,
            "employees": employees,
            "incomplete_users": incomplete_users,
            "visitor_sessions": visitor_sessions,
            "financial_year": financial_year,
            "assessment_year": assessment_year,
            "fee_amount": (
                settings.fee_amount
                if settings and settings.fee_amount is not None
                else 150.0
            ),
            "upi_id": (
                settings.upi_id
                if settings and settings.upi_id
                else ""
            ),
            "telegram_bot_token": (
                settings.telegram_bot_token
                if settings and settings.telegram_bot_token
                else ""
            ),
            "telegram_chat_id": (
                settings.telegram_chat_id
                if settings and settings.telegram_chat_id
                else ""
            ),
            "stats": {
                "total_users": db.query(User).count(),
                "unique_visitors": unique_visitors,
                "form_started": form_started,
                "incomplete_users": len(incomplete_users),
                "payment_page_reached": payment_page_count,
                "total_revenue": total_revenue,
                "pending_payments": pending_count,
                "approved_payments": approved_count,
                "rejected_payments": rejected_count,
                "admin_generated": len(admin_generated),
                "email_sent": email_sent_count,
                "email_failed": email_failed_count,
            },
            "csrf_token": csrf_token,
        },
    )
    response.set_cookie("csrf_token", csrf_token, max_age=ADMIN_SESSION_MAX_AGE, secure=request_uses_https(request), httponly=False, samesite="strict")
    return response


# =========================================================
# ADMIN LIVE DASHBOARD API
# =========================================================

def _admin_live_payment_row(
    payment: Payment,
    user: Optional[User],
    employee: Optional[EmployeeDetail],
) -> dict:
    """
    JSON-safe payment representation for the admin dashboard background
    sync. No settings, tokens or other secrets are exposed here.
    """

    return {
        "payment_id": payment.id,
        "user_id": payment.user_id,
        "name": (
            employee.name
            if employee and employee.name
            else "Unknown Employee"
        ),
        "pan": (
            employee.pan
            if employee and employee.pan
            else "N/A"
        ),
        "email": user.email if user else None,
        "mobile": user.mobile if user else None,
        "utr_number": payment.upi_txn_utr or "",
        "amount": float(payment.amount or 0),
        "status": payment.status or "pending",
        "created_at": (
            payment.created_at.isoformat()
            if payment.created_at
            else None
        ),
        "approved_at": (
            payment.approved_at.isoformat()
            if payment.approved_at
            else None
        ),
    }


def _admin_live_incomplete_row(
    session: VisitorSession,
    employee: Optional[EmployeeDetail],
) -> dict:
    snapshot = session.form_snapshot_json or {}

    return {
        "session_id": session.id,
        "user_id": session.user_id,
        "name": (
            employee.name
            if employee and employee.name
            else snapshot.get("name")
        ),
        "email": session.email,
        "mobile": session.mobile,
        "current_page": session.current_page,
        "current_step": session.current_step,
        "last_completed_step": session.last_completed_step,
        "progress_percent": int(session.progress_percent or 0),
        "application_status": session.application_status,
        "payment_status": session.payment_status,
        "reminder_status": session.reminder_status,
        "last_seen_at": (
            session.last_seen_at.isoformat()
            if session.last_seen_at
            else None
        ),
    }


@app.get("/api/admin/dashboard/live")
def admin_dashboard_live(
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    """
    Authenticated live snapshot for the admin dashboard.

    The browser polls this endpoint in the background and updates only
    changed counters/tables. The page itself is never reloaded, so typing
    into admin settings is not interrupted and unsaved values are safe.
    """

    require_admin_session(admin_session)

    payments = (
        db.query(Payment)
        .order_by(Payment.created_at.desc())
        .all()
    )

    visitor_sessions = (
        db.query(VisitorSession)
        .order_by(VisitorSession.last_seen_at.desc())
        .all()
    )

    # Batch related user/employee lookups. This endpoint runs repeatedly,
    # so avoid two extra SQL queries for every payment/session row.
    related_user_ids = {
        item.user_id
        for item in payments
        if item.user_id
    }
    related_user_ids.update(
        item.user_id
        for item in visitor_sessions
        if item.user_id
    )

    users_by_id = {}
    employees_by_user_id = {}

    if related_user_ids:
        users_by_id = {
            item.id: item
            for item in (
                db.query(User)
                .filter(User.id.in_(related_user_ids))
                .all()
            )
        }

        employees_by_user_id = {
            item.user_id: item
            for item in (
                db.query(EmployeeDetail)
                .filter(EmployeeDetail.user_id.in_(related_user_ids))
                .all()
            )
        }

    payment_rows = [
        _admin_live_payment_row(
            payment,
            users_by_id.get(payment.user_id),
            employees_by_user_id.get(payment.user_id),
        )
        for payment in payments
    ]

    pending_count = sum(
        1 for item in payment_rows
        if item["status"] == "pending"
    )
    approved_count = sum(
        1 for item in payment_rows
        if item["status"] == "approved"
    )
    rejected_count = sum(
        1 for item in payment_rows
        if item["status"] == "rejected"
    )
    total_revenue = sum(
        float(item["amount"] or 0)
        for item in payment_rows
        if item["status"] == "approved"
    )

    incomplete_rows = []

    for session in visitor_sessions:
        if session.application_status == "completed":
            continue

        if session.payment_status == "approved":
            continue

        incomplete_rows.append(
            _admin_live_incomplete_row(
                session,
                employees_by_user_id.get(session.user_id),
            )
        )

    unique_visitors = (
        db.query(VisitorSession.visitor_id)
        .distinct()
        .count()
    )

    form_started = db.query(VisitorSession).count()

    payment_page_count = (
        db.query(VisitorSession)
        .filter(VisitorSession.progress_percent >= 80)
        .count()
    )

    admin_generated_count = (
        db.query(Form16Generation)
        .filter(Form16Generation.source == "admin")
        .count()
    )

    email_sent_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "sent")
        .count()
    )

    email_failed_count = (
        db.query(EmailDelivery)
        .filter(EmailDelivery.status == "failed")
        .count()
    )

    settings = db.query(AdminSettings).first()
    financial_year = (
        settings.financial_year
        if settings and settings.financial_year
        else None
    )

    assessment_year = None
    if financial_year:
        try:
            assessment_year = assessment_year_from_financial_year(
                financial_year
            )
        except ValueError:
            assessment_year = None

    return {
        "server_time": datetime.utcnow().isoformat(),
        "financial_year": financial_year,
        "assessment_year": assessment_year,
        "payments": payment_rows,
        "incomplete_users": incomplete_rows,
        "stats": {
            "total_users": db.query(User).count(),
            "unique_visitors": unique_visitors,
            "form_started": form_started,
            "incomplete_users": len(incomplete_rows),
            "payment_page_reached": payment_page_count,
            "total_revenue": total_revenue,
            "pending_payments": pending_count,
            "approved_payments": approved_count,
            "rejected_payments": rejected_count,
            "admin_generated": admin_generated_count,
            "email_sent": email_sent_count,
            "email_failed": email_failed_count,
        },
    }


# =========================================================
# ADMIN PAYMENT ACTIONS
# =========================================================

@app.post("/admin/payment/approve/{payment_id}")
def approve_payment(
    payment_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "approved"
    payment.approved_at = datetime.utcnow()

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "approved",
        application_status="completed",
    )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    db.commit()

    enqueue_form16_job(db, generation, "payment_confirmed_form16")
    db.commit()

    return RedirectResponse(
        url="/admin/dashboard",
        status_code=303,
    )


@app.post("/admin/payment/decline/{payment_id}")
def decline_payment(
    payment_id: str,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "rejected"
    payment.approved_at = None

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "rejected",
        application_status="payment_pending",
    )

    db.commit()

    return RedirectResponse(
        url="/admin/dashboard",
        status_code=303,
    )


@app.get("/api/admin/approve/{payment_id}")
def admin_approve_payment(
    payment_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:
        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    payment.status = "approved"
    payment.approved_at = datetime.utcnow()

    sync_visitor_payment_state(
        db,
        payment.user_id,
        "approved",
        application_status="completed",
    )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    db.commit()

    enqueue_form16_job(db, generation, "payment_confirmed_form16")
    db.commit()

    return {
        "message": f"Payment {payment_id} successfully approved.",
        "generation_id": generation.id,
    }


# =========================================================
# ADMIN DIRECT FORM-16 GENERATION
# =========================================================

@app.post("/admin/form16/generate/{user_id}")
def admin_generate_form16(
    user_id: str,
    background_tasks: BackgroundTasks,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    if not is_valid_admin_session(admin_session):
        return RedirectResponse(
            url="/admin",
            status_code=303,
        )

    employee = (
        db.query(EmployeeDetail)
        .filter(EmployeeDetail.user_id == user_id)
        .first()
    )

    if not employee:
        raise HTTPException(
            status_code=404,
            detail="Employee details not found.",
        )

    user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not user or not user.email:
        raise HTTPException(
            status_code=400,
            detail="Employee email is required before admin generation.",
        )

    if not employee.tan_id:
        raise HTTPException(
            status_code=400,
            detail="Employer TAN is missing for this employee.",
        )

    financial_year = get_active_financial_year(db)

    # Validate all required PDF data before creating the audit record.
    build_form16_payload(
        db,
        user_id,
        financial_year,
    )

    generation = Form16Generation(
        id=str(uuid.uuid4()),
        user_id=user_id,
        payment_id=None,
        financial_year=financial_year,
        assessment_year=assessment_year_from_financial_year(
            financial_year
        ),
        source="admin",
        status="queued",
    )

    db.add(generation)
    db.commit()

    enqueue_form16_job(db, generation, "admin_generated_form16")
    db.commit()

    return RedirectResponse(url="/admin/dashboard", status_code=303)


# =========================================================
# TELEGRAM
# =========================================================

def send_telegram_notification(
    payment_id: str,
    utr: str,
    user_id: str,
):

    # Background task gets its own DB session.
    db = SessionLocal()

    try:

        admin_settings = (
            db.query(AdminSettings)
            .first()
        )

        if (
            not admin_settings
            or not admin_settings.telegram_bot_token
            or not admin_settings.telegram_chat_id
        ):
            return

        employee = (
            db.query(EmployeeDetail)
            .filter(
                EmployeeDetail.user_id == user_id
            )
            .first()
        )

        emp_name = (
            employee.name
            if employee
            else "Unknown User"
        )

        emp_pan = (
            employee.pan
            if employee
            else "Unknown PAN"
        )

        base_url = (
            os.getenv("PUBLIC_BASE_URL")
            or os.getenv("RENDER_EXTERNAL_URL")
            or ""
        ).rstrip("/")

        approve_link = (
            f"{base_url}/admin/dashboard"
            if base_url
            else "/admin/dashboard"
        )

        message = (
            "New Form 16 Payment Request\n\n"
            f"Name: {emp_name}\n"
            f"PAN: {emp_pan}\n"
            f"UTR No: {utr}\n\n"
            f"Approve: {approve_link}"
        )

        url = (
            "https://api.telegram.org/bot"
            f"{admin_settings.telegram_bot_token}"
            "/sendMessage"
        )

        payload = {
            "chat_id": (
                admin_settings.telegram_chat_id
            ),
            "text": message,
            "disable_web_page_preview": True,
        }

        requests.post(
            url,
            json=payload,
            timeout=5,
        )

    except Exception as exc:

        print(
            f"Telegram Notification Failed: {exc}"
        )

    finally:
        db.close()


# =========================================================
# PAYMENT-SCOPED ACCESS TOKENS
# =========================================================
def _payment_access_secret() -> str:
    return os.getenv("PAYMENT_ACCESS_SECRET", "").strip() or _admin_session_secret()

def _payment_access_token(payment_id: str, ttl_seconds: int = 7 * 24 * 60 * 60) -> str:
    exp = str(int(time.time()) + ttl_seconds); raw = f"{payment_id}.{exp}"
    sig = hmac.new(_payment_access_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"

def _payment_access_ok(payment_id: str, token: Optional[str]) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        if int(exp) < int(time.time()): return False
        raw = f"{payment_id}.{exp}"
        expected = hmac.new(_payment_access_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

# =========================================================
# PAYMENT SUBMISSION
# =========================================================

@app.post("/api/payment/submit")
async def submit_utr(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):

    data = await request.json()

    user_id = str(
        data.get("user_id") or ""
    ).strip()

    utr_number = str(
        data.get("utr_number") or ""
    ).strip()

    amount = data.get("amount")

    if not user_id:
        raise HTTPException(
            status_code=400,
            detail="User ID is required.",
        )

    require_journey_user(request, db, user_id)

    if not utr_number:
        raise HTTPException(
            status_code=400,
            detail="UTR number is required.",
        )

    try:
        amount = float(amount)

    except (TypeError, ValueError):

        raise HTTPException(
            status_code=400,
            detail="Valid payment amount is required.",
        )

    if amount <= 0:

        raise HTTPException(
            status_code=400,
            detail="Payment amount must be positive.",
        )

    # Payment submission must NOT delete or rebuild salary
    # ledgers. Ledger persistence belongs to ledger workflow.

    base_user = (
        db.query(User)
        .filter(User.id == user_id)
        .first()
    )

    if not base_user:

        base_user = User(id=user_id)
        db.add(base_user)
        db.flush()

    payment_id = str(uuid.uuid4())

    payment = Payment(
        id=payment_id,
        user_id=user_id,
        amount=amount,
        upi_txn_utr=utr_number,
        status="pending",
    )

    db.add(payment)

    try:

        sync_visitor_payment_state(
            db,
            user_id,
            "pending",
            application_status="payment_submitted",
        )
        db.commit()

    except IntegrityError as exc:

        db.rollback()

        error_text = str(exc.orig).upper()

        if "UNIQUE" in error_text:

            raise HTTPException(
                status_code=400,
                detail=(
                    "This UTR number has already been used."
                ),
            )

        raise HTTPException(
            status_code=400,
            detail="Payment could not be saved.",
        )

    background_tasks.add_task(
        send_telegram_notification,
        payment_id,
        utr_number,
        user_id,
    )

    return {
        "message": "UTR submitted successfully",
        "payment_id": payment_id,
        "access_token": _payment_access_token(payment_id),
    }


@app.get(
    "/api/payment/status/{payment_id}"
)
def check_payment_status(
    payment_id: str,
    access_token: str = "",
    db: Session = Depends(get_db),
):

    if not _payment_access_ok(payment_id, access_token):
        raise HTTPException(status_code=401, detail="Payment verification required.")

    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment:

        raise HTTPException(
            status_code=404,
            detail="Payment not found.",
        )

    return {
        "status": payment.status,
    }


# =========================================================
# FORM 16 DOWNLOAD
# =========================================================

def mark_generation_downloaded(
    db: Session,
    generation: Form16Generation,
):
    now = datetime.utcnow()
    generation.download_count = int(generation.download_count or 0) + 1

    if not generation.first_downloaded_at:
        generation.first_downloaded_at = now

    generation.last_downloaded_at = now

    sessions = (
        db.query(VisitorSession)
        .filter(VisitorSession.user_id == generation.user_id)
        .all()
    )

    for session in sessions:
        add_visitor_event(
            db,
            session,
            "form16_downloaded",
            page_name="completed",
            step_name="download",
            event_data={
                "generation_id": generation.id,
                "source": generation.source,
            },
        )

    db.commit()


@app.get("/api/download/form16-generation/{generation_id}")
def download_generation_pdf(
    generation_id: str,
    admin_session: str = Cookie(None),
    db: Session = Depends(get_db),
):
    require_admin_session(admin_session)
    generation = (
        db.query(Form16Generation)
        .filter(Form16Generation.id == generation_id)
        .first()
    )

    if not generation:
        raise HTTPException(
            status_code=404,
            detail="Form 16 generation record not found.",
        )

    if generation.status == "generated" and generation.pdf_blob:
        mark_generation_downloaded(db, generation)
        return StreamingResponse(io.BytesIO(generation.pdf_blob), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{generation.file_name or "Form16.pdf"}"'})
    if generation.status == "generated" and generation.storage_path and os.path.isfile(generation.storage_path):
        pdf_path, filename = generation.storage_path, (generation.file_name or "Form16.pdf")
    else:
        raise HTTPException(status_code=409, detail={"status": generation.status, "message": "Form 16 is queued for generation. Please try again shortly."})

    mark_generation_downloaded(
        db,
        generation,
    )

    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
    )


@app.get("/api/download/form16/{payment_id}")
def download_pdf(
    payment_id: str,
    access_token: str = "",
    db: Session = Depends(get_db),
):
    if not _payment_access_ok(payment_id, access_token):
        raise HTTPException(status_code=401, detail="Payment verification required.")
    payment = (
        db.query(Payment)
        .filter(Payment.id == payment_id)
        .first()
    )

    if not payment or payment.status != "approved":
        raise HTTPException(
            status_code=403,
            detail="Payment not approved yet.",
        )

    financial_year = get_active_financial_year(db)

    generation = get_or_create_generation(
        db,
        user_id=payment.user_id,
        financial_year=financial_year,
        source="user_payment",
        payment_id=payment.id,
    )

    enqueue_form16_job(db, generation, "payment_confirmed_form16")
    db.commit()
    if generation.status == "generated" and generation.pdf_blob:
        mark_generation_downloaded(db, generation)
        return StreamingResponse(io.BytesIO(generation.pdf_blob), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{generation.file_name or "Form16.pdf"}"'})
    if not (generation.status == "generated" and generation.storage_path and os.path.isfile(generation.storage_path)):
        raise HTTPException(status_code=409, detail={"status": generation.status, "message": "Form 16 is queued for generation. Please try again shortly."})
    pdf_path, filename = generation.storage_path, (generation.file_name or "Form16.pdf")

    mark_generation_downloaded(
        db,
        generation,
    )

    return FileResponse(
        path=pdf_path,
        filename=filename,
        media_type="application/pdf",
    )

# =========================================================
# RETURNING USER + SECURE FORM 16 ARCHIVE
# =========================================================
def _normalise_pan(pan: str) -> str:
    value = re.sub(r"\s+", "", pan or "").upper()
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", value):
        raise HTTPException(400, "Valid PAN is required.")
    return value

def _mask_email(value: str) -> str:
    if not value or "@" not in value: return ""
    a,b=value.split("@",1); return (a[:1]+"***@"+b)

def _profile_token(user_id: str) -> str:
    exp=str(int(time.time())+1800); raw=f"{user_id}.{exp}"
    sig=hmac.new(_admin_session_secret().encode(), raw.encode(), hashlib.sha256).hexdigest()
    return f"{raw}.{sig}"

def _profile_token_user(token: Optional[str]) -> Optional[str]:
    try:
        uid,exp,sig=(token or "").split(".",2); raw=f"{uid}.{exp}"
        if int(exp)<int(time.time()): return None
        expected=hmac.new(_admin_session_secret().encode(),raw.encode(),hashlib.sha256).hexdigest()
        return uid if hmac.compare_digest(sig,expected) else None
    except Exception: return None

@app.post("/api/profile/lookup")
def returning_profile_lookup(payload: Dict[str, Any], db: Session = Depends(get_db)):
    pan=_normalise_pan(str(payload.get("pan", "")))
    employee=db.query(EmployeeDetail).filter(EmployeeDetail.pan==pan).first()
    if not employee: return {"found":False,"verification_required":False}
    user=db.query(User).filter(User.id==employee.user_id).first()
    return {"found":True,"verification_required":True,"masked_email":_mask_email(user.email if user else ""),"has_mobile":bool(user and user.mobile)}

@app.post("/api/profile/request-otp")
def returning_profile_request_otp(payload: Dict[str, Any], db: Session = Depends(get_db)):
    pan=_normalise_pan(str(payload.get("pan", "")))
    employee=db.query(EmployeeDetail).filter(EmployeeDetail.pan==pan).first()
    if not employee: return {"ok":True}
    user=db.query(User).filter(User.id==employee.user_id).first()
    if not user or not user.email: raise HTTPException(409,"No verified email is available for this profile.")
    cid=str(uuid.uuid4()); otp=f"{secrets.randbelow(1000000):06d}"
    db.add(ProfileAccessChallenge(id=cid,user_id=user.id,otp_hash=_otp_hash(cid,otp),expires_at=datetime.utcnow()+timedelta(minutes=10)))
    db.commit()
    send_email_message(user.email,"VetanKosh verification OTP",f"Your VetanKosh verification OTP is {otp}. It expires in 10 minutes.")
    return {"ok":True,"challenge_id":cid,"masked_email":_mask_email(user.email)}

@app.post("/api/profile/verify-otp")
def returning_profile_verify_otp(payload: Dict[str, Any], db: Session = Depends(get_db)):
    cid=str(payload.get("challenge_id", "")); otp=str(payload.get("otp", "")).strip(); now=datetime.utcnow()
    row=db.query(ProfileAccessChallenge).filter(ProfileAccessChallenge.id==cid).first()
    if not row or row.consumed_at or row.expires_at<=now or row.attempts>=5: raise HTTPException(401,"OTP expired or invalid.")
    row.attempts+=1
    if not hmac.compare_digest(row.otp_hash,_otp_hash(row.id,otp)):
        db.commit(); raise HTTPException(401,"OTP invalid.")
    row.consumed_at=now; db.commit()
    token=_profile_token(row.user_id)
    return {"ok":True,"access_token":token}

@app.get("/api/profile/me")
def returning_profile_me(access_token: str, db: Session = Depends(get_db)):
    uid=_profile_token_user(access_token)
    if not uid: raise HTTPException(401,"Verification required.")
    employee=db.query(EmployeeDetail).filter(EmployeeDetail.user_id==uid).first(); user=db.query(User).filter(User.id==uid).first()
    employer=db.query(EmployerCache).filter(EmployerCache.tan==employee.tan_id).first() if employee and employee.tan_id else None
    return {"user_id":uid,"name":employee.name if employee else "","pan":employee.pan if employee else "","office_school_name":employee.office_school_name if employee else "","email":user.email if user else "","mobile":user.mobile if user else "","tan":employee.tan_id if employee else "","employer":({"tan":employer.tan,"officer_name":employer.officer_name,"officer_father_name":employer.officer_father_name,"designation":employer.designation,"employer_name":employer.employer_name,"employer_address":employer.employer_address,"pan":employer.pan,"cit_tds_address":employer.cit_tds_address,"cit_tds_city":employer.cit_tds_city,"cit_tds_pincode":employer.cit_tds_pincode} if employer else None)}

@app.get("/api/profile/form16")
def returning_form16_list(access_token: str, db: Session = Depends(get_db)):
    uid=_profile_token_user(access_token)
    if not uid: raise HTTPException(401,"Verification required.")
    rows=db.query(Form16Generation).filter(Form16Generation.user_id==uid,Form16Generation.status=="generated").order_by(Form16Generation.generated_at.desc()).all()
    return [{"id":r.id,"financial_year":r.financial_year,"assessment_year":r.assessment_year,"generated_at":r.generated_at,"version":r.version_no,"latest":r.is_latest,"download_available":bool(r.pdf_blob or (r.storage_path and os.path.isfile(r.storage_path)))} for r in rows]

@app.get("/api/profile/form16/{generation_id}/download")
def returning_form16_download(generation_id: str, access_token: str, db: Session = Depends(get_db)):
    uid=_profile_token_user(access_token)
    if not uid: raise HTTPException(401,"Verification required.")
    row=db.query(Form16Generation).filter(Form16Generation.id==generation_id,Form16Generation.user_id==uid,Form16Generation.status=="generated").first()
    if not row or not (row.pdf_blob or (row.storage_path and os.path.isfile(row.storage_path))): raise HTTPException(404,"Archived PDF is not available.")
    db.add(Form16ArchiveAccess(id=str(uuid.uuid4()),generation_id=row.id,action="download")); mark_generation_downloaded(db,row); db.commit()
    if row.pdf_blob:
        return StreamingResponse(io.BytesIO(row.pdf_blob), media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{row.file_name or "Form16.pdf"}"'})
    return FileResponse(row.storage_path,filename=row.file_name or "Form16.pdf",media_type="application/pdf")

@app.get("/robots.txt", response_class=HTMLResponse)
def robots():
    return "User-agent: *\nDisallow: /admin\nDisallow: /api/\n"

@app.get("/api/admin/security/sessions")
def admin_security_sessions(admin_session: str = Cookie(None), db: Session = Depends(get_db)):
    require_admin_session(admin_session); now=datetime.utcnow()
    rows=db.query(AdminSession).filter(AdminSession.revoked_at.is_(None),AdminSession.expires_at>now).order_by(AdminSession.created_at.desc()).all()
    return [{"id":r.id,"created_at":r.created_at,"last_seen_at":r.last_seen_at,"expires_at":r.expires_at,"user_agent":r.user_agent} for r in rows]

@app.post("/api/admin/security/sessions/{session_id}/revoke")
def admin_revoke_session(session_id: str, admin_session: str = Cookie(None), db: Session = Depends(get_db)):
    require_admin_session(admin_session); row=db.query(AdminSession).filter(AdminSession.id==session_id).first()
    if not row: raise HTTPException(404,"Session not found.")
    row.revoked_at=datetime.utcnow(); db.commit(); return {"ok":True}

# =========================================================
# RAZORPAY (optional; manual UTR remains available)
# =========================================================
def _razorpay_enabled(): return os.getenv("RAZORPAY_ENABLED","false").lower() in {"1","true","yes"}
def _rz_keys():
    kid=os.getenv("RAZORPAY_KEY_ID","").strip(); sec=os.getenv("RAZORPAY_KEY_SECRET","").strip()
    if not (_razorpay_enabled() and kid and sec): raise HTTPException(503,"Online gateway is not configured.")
    return kid,sec

@app.get("/api/payment/gateway/config")
@app.get("/api/payment/gateway-config")
def gateway_config():
    return {"enabled":_razorpay_enabled(),"key_id":os.getenv("RAZORPAY_KEY_ID","") if _razorpay_enabled() else "","provider":"razorpay"}

@app.post("/api/payment/gateway/order")
def gateway_order(payload: Dict[str,Any], request: Request, db: Session=Depends(get_db)):
    kid,sec=_rz_keys(); uid=str(payload.get("user_id","")).strip()
    require_journey_user(request, db, uid)
    if not db.query(User).filter(User.id==uid).first(): raise HTTPException(404,"User not found.")
    settings=db.query(AdminSettings).first(); amount=float(settings.fee_amount if settings and settings.fee_amount is not None else 150.0)
    receipt="kt_"+uuid.uuid4().hex[:24]
    r=requests.post("https://api.razorpay.com/v1/orders",auth=(kid,sec),json={"amount":int(round(amount*100)),"currency":"INR","receipt":receipt},timeout=20)
    if r.status_code>=300: raise HTTPException(502,"Payment gateway order creation failed.")
    order=r.json(); payment=Payment(id=str(uuid.uuid4()),user_id=uid,amount=amount,status="pending"); db.add(payment); db.flush()
    gp=GatewayPayment(id=str(uuid.uuid4()),payment_id=payment.id,provider="razorpay",mode=os.getenv("RAZORPAY_MODE","live"),currency="INR",provider_order_id=order["id"],status="pending",metadata_json={"receipt":receipt})
    db.add(gp); db.commit()
    return {"payment_id":payment.id,"payment_record_id":payment.id,"order_id":order["id"],"amount":order["amount"],"amount_paise":order["amount"],"currency":"INR","key_id":kid,"access_token":_payment_access_token(payment.id)}

@app.post("/api/payment/gateway/verify")
def gateway_verify(payload: Dict[str,Any], db: Session=Depends(get_db)):
    _,sec=_rz_keys(); order_id=str(payload.get("razorpay_order_id","")); provider_payment_id=str(payload.get("razorpay_payment_id","")); supplied=str(payload.get("razorpay_signature",""))
    expected=hmac.new(sec.encode(),f"{order_id}|{provider_payment_id}".encode(),hashlib.sha256).hexdigest()
    if not supplied or not hmac.compare_digest(expected,supplied): raise HTTPException(400,"Payment signature verification failed.")
    gp=db.query(GatewayPayment).filter(GatewayPayment.provider_order_id==order_id).first()
    if not gp: raise HTTPException(404,"Gateway order not found.")
    gp.provider_payment_id=provider_payment_id; gp.status="verified"; gp.verified_at=datetime.utcnow(); payment=db.query(Payment).filter(Payment.id==gp.payment_id).first(); payment.status="approved"; payment.approved_at=datetime.utcnow()
    sync_visitor_payment_state(db, payment.user_id, "approved", application_status="completed")
    generation=get_or_create_generation(db,payment.user_id,get_active_financial_year(db),"user_payment",payment.id)
    enqueue_form16_job(db,generation,"payment_confirmed_form16"); db.commit()
    return {"ok":True,"payment_id":payment.id,"status":"approved","generation_id":generation.id,"access_token":_payment_access_token(payment.id)}

@app.post("/api/payment/gateway/webhook")
async def gateway_webhook(request: Request, db: Session=Depends(get_db)):
    secret=os.getenv("RAZORPAY_WEBHOOK_SECRET","").strip()
    if not secret: raise HTTPException(503,"Webhook secret is not configured.")
    body=await request.body(); supplied=request.headers.get("x-razorpay-signature",""); expected=hmac.new(secret.encode(),body,hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected,supplied): raise HTTPException(401,"Invalid webhook signature.")
    data=await request.json(); event_id=request.headers.get("x-razorpay-event-id") or hashlib.sha256(body).hexdigest(); existing=db.query(PaymentWebhookEvent).filter(PaymentWebhookEvent.provider_event_id==event_id).first()
    if existing: return {"ok":True,"duplicate":True}
    event=str(data.get("event","")); pe=PaymentWebhookEvent(id=str(uuid.uuid4()),provider="razorpay",provider_event_id=event_id,event_type=event,payload_hash=hashlib.sha256(body).hexdigest(),event_data_json={"event":event},status="received"); db.add(pe)
    entity=((data.get("payload") or {}).get("payment") or {}).get("entity") or {}; order_id=entity.get("order_id"); pid=entity.get("id")
    if event in {"payment.captured","order.paid"} and order_id:
        gp=db.query(GatewayPayment).filter(GatewayPayment.provider_order_id==order_id).first()
        if gp:
            gp.provider_payment_id=pid or gp.provider_payment_id; gp.status="verified"; gp.verified_at=datetime.utcnow(); pay=db.query(Payment).filter(Payment.id==gp.payment_id).first()
            if pay:
                pay.status="approved"; pay.approved_at=datetime.utcnow(); sync_visitor_payment_state(db,pay.user_id,"approved",application_status="completed")
                generation=get_or_create_generation(db,pay.user_id,get_active_financial_year(db),"user_payment",pay.id)
                enqueue_form16_job(db,generation,"payment_confirmed_form16")
            pe.gateway_payment_id=gp.id; pe.status="processed"; pe.processed_at=datetime.utcnow()
        else: pe.status="ignored"; pe.processed_at=datetime.utcnow()
    else: pe.status="ignored"; pe.processed_at=datetime.utcnow()
    db.commit(); return {"ok":True}
