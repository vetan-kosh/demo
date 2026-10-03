import calendar
import os
import re
import tempfile
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from urllib.request import Request, urlopen

from jinja2 import Environment, FileSystemLoader
from weasyprint import CSS, HTML
from weasyprint.text.fonts import FontConfiguration

from core.tax_calculator import compute_annual_tax, compute_slab_tax


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
TEMPLATE_NAME = "form16_template.html"

# Reuse the Jinja environment/template for every job in the long-lived worker.
# Rebuilding these objects for every PDF adds avoidable CPU and filesystem I/O.
_TEMPLATE_ENV = Environment(
    loader=FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
    auto_reload=False,
    cache_size=50,
)
_FORM16_TEMPLATE = _TEMPLATE_ENV.get_template(TEMPLATE_NAME)

# The production PDF server may not have a Devanagari font installed.
# Fetch an OFL-licensed Noto Sans Devanagari UI font once into the runtime
# temp directory and explicitly embed it in every generated PDF.
DEVANAGARI_FONT_URL = (
    "https://raw.githubusercontent.com/google/fonts/main/"
    "ofl/notosansdevanagariui/NotoSansDevanagariUI-Regular.ttf"
)
DEVANAGARI_FONT_DIR = os.path.join(
    tempfile.gettempdir(),
    "form16_fonts",
)
DEVANAGARI_FONT_PATH = os.path.join(
    DEVANAGARI_FONT_DIR,
    "NotoSansDevanagariUI-Regular.ttf",
)


@lru_cache(maxsize=1)
def _ensure_devanagari_font():
    """Return a usable local Devanagari font path, downloading it if needed."""
    try:
        # Prefer an OS-installed font in production. This avoids network I/O
        # and keeps PDF generation deterministic on the VPS.
        system_candidates = (
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/opentype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagariUI-Regular.ttf",
        )
        for candidate in system_candidates:
            if os.path.isfile(candidate) and os.path.getsize(candidate) > 50_000:
                return candidate

        if (
            os.path.isfile(DEVANAGARI_FONT_PATH)
            and os.path.getsize(DEVANAGARI_FONT_PATH) > 50_000
        ):
            return DEVANAGARI_FONT_PATH

        os.makedirs(DEVANAGARI_FONT_DIR, exist_ok=True)
        temp_path = DEVANAGARI_FONT_PATH + ".download"

        request = Request(
            DEVANAGARI_FONT_URL,
            headers={"User-Agent": "Form16-PDF-Renderer/1.0"},
        )
        with urlopen(request, timeout=20) as response, open(
            temp_path, "wb"
        ) as font_file:
            font_file.write(response.read())

        if os.path.getsize(temp_path) <= 50_000:
            raise RuntimeError("Downloaded Devanagari font is unexpectedly small.")

        os.replace(temp_path, DEVANAGARI_FONT_PATH)
        return DEVANAGARI_FONT_PATH
    except Exception as exc:
        print(f"WARNING: Devanagari font setup failed: {exc}")
        return None


@lru_cache(maxsize=1)
def _pdf_font_resources():
    """Build WeasyPrint font resources without touching page/layout CSS."""
    font_config = FontConfiguration()
    font_path = _ensure_devanagari_font()

    if not font_path:
        return font_config, []

    font_uri = Path(font_path).resolve().as_uri()
    font_css = CSS(
        string=f"""
        @font-face {{
            font-family: 'Form16 Devanagari';
            src: url('{font_uri}') format('truetype');
            font-style: normal;
            font-weight: 100 900;
        }}

        html, body, table, thead, tbody, tfoot, tr, th, td,
        div, span, p, strong, b, small {{
            font-family: 'Liberation Sans', 'DejaVu Sans',
                         'Form16 Devanagari', sans-serif !important;
        }}
        """,
        font_config=font_config,
    )
    return font_config, [font_css]


MONTH_NAMES = {
    1: "January",
    2: "February",
    3: "March",
    4: "April",
    5: "May",
    6: "June",
    7: "July",
    8: "August",
    9: "September",
    10: "October",
    11: "November",
    12: "December",
}


def _number(value):
    """Safely convert DB/template values to float."""
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _money(value):
    return f"{_number(value):.2f}"


def _validate_financial_year(financial_year):
    value = str(financial_year or "").strip()

    match = re.fullmatch(r"(\d{4})-(\d{2})", value)
    if not match:
        raise ValueError(
            "Financial year must be in YYYY-YY format, for example 2025-26."
        )

    start_year = int(match.group(1))
    expected_end = (start_year + 1) % 100

    if int(match.group(2)) != expected_end:
        raise ValueError(
            f"Invalid financial year {value}. Expected "
            f"{start_year}-{expected_end:02d}."
        )

    return value, start_year, start_year + 1


def _assessment_year(financial_year):
    _, start_year, _ = _validate_financial_year(financial_year)
    return f"{start_year + 1}-{(start_year + 2) % 100:02d}"


def _fy_period_dates(financial_year):
    """
    Project rule: the payroll/Form-16 cycle is March -> February.

    FY 2025-26 -> 01.03.2025 through 28.02.2026
    FY 2027-28 -> 01.03.2027 through 29.02.2028
    """
    _, start_year, end_year = _validate_financial_year(financial_year)
    feb_last_day = calendar.monthrange(end_year, 2)[1]

    return (
        f"01.03.{start_year}",
        f"{feb_last_day:02d}.02.{end_year}",
    )


def _normalise_key(value):
    return re.sub(
        r"[^a-z0-9_]+",
        "_",
        str(value or "").strip().lower(),
    ).strip("_")


def _map_value(values, *names):
    values = values or {}

    normalised = {
        _normalise_key(key): value
        for key, value in values.items()
    }

    for name in names:
        key = _normalise_key(name)
        if key in normalised:
            return _number(normalised.get(key))

    return 0.0


def _sum_numeric_map(values):
    return sum(
        _number(value)
        for value in (values or {}).values()
    )


def _regular_component_extras(line_items):
    """
    Sum earnings stored in line_items without counting Basic/DA/HRA twice.

    MonthlyLedger already stores Basic/DA/HRA in dedicated columns. The
    parser also keeps those values in line_items_json, so those canonical
    heads must be excluded from the extra-earnings sum.
    """
    excluded = {
        "basic",
        "basic_pay",
        "da",
        "dearness_allowance",
        "hra",
        "house_rent_allowance",
    }

    total = 0.0

    for raw_key, raw_value in (line_items or {}).items():
        key = _normalise_key(raw_key)
        if key in excluded:
            continue
        total += _number(raw_value)

    return total


def _arrear_gross_from_flags(flags):
    """
    Backend stores one fingerprinted flag per arrear:
    arrear_gross:<amount>:<fingerprint>
    """
    total = 0.0

    for raw_flag in (flags or []):
        flag = str(raw_flag or "").strip()

        if not flag.startswith("arrear_gross:"):
            continue

        parts = flag.split(":", 2)
        if len(parts) < 2:
            continue

        total += _number(parts[1])

    return round(total, 2)


def _arrear_component_total(line_items):
    total = 0.0

    for raw_key, raw_value in (line_items or {}).items():
        if _normalise_key(raw_key).startswith("arrear_"):
            total += _number(raw_value)

    return round(total, 2)


def _arrear_da_total(line_items):
    return (
        _map_value(
            line_items,
            "arrear_da",
            "arrear_dearness_allowance",
        )
    )


def _deduction_total_for_head(deductions, *names):
    """
    Return regular + arrear deduction for one displayed deduction head.

    Example:
      gpf + arrear_gpf
      income_tax_tds + arrear_income_tax_tds
    """
    regular = _map_value(deductions, *names)

    arrear_names = [
        f"arrear_{_normalise_key(name)}"
        for name in names
    ]

    arrear = _map_value(deductions, *arrear_names)

    return regular + arrear


def _prepare_ledger(ledger):
    """
    Convert MonthlyLedger dictionaries into template-ready rows.

    Important:
    - Projection is NOT done here; main.py owns Jan/Feb materialisation.
    - Arrear remains allocated to its actual payment month.
    - Combined-period rows are labelled instead of being presented as an
      ordinary single-month salary.
    """
    formatted = []

    totals = {
        "basic": 0.0,
        "da": 0.0,
        "hra": 0.0,
        "ta": 0.0,
        "medical": 0.0,
        "ta_da": 0.0,
        "gross": 0.0,
        "arrear": 0.0,
        "arrear_da": 0.0,
        "gpf": 0.0,
        "gli": 0.0,
        "prof_tax": 0.0,
        "tds": 0.0,
        "total_deduction": 0.0,
        "net_pay": 0.0,
    }

    for row in (ledger or []):
        month = int(row.get("month", 0) or 0)
        year = int(row.get("year", 0) or 0)

        line_items = row.get("line_items") or {}
        deductions = row.get("deductions") or {}
        flags = row.get("flags") or []
        source = str(row.get("source") or "").strip().lower()

        basic = _number(row.get("basic_pay"))
        da = _number(row.get("da"))
        hra = _number(row.get("hra"))

        ta = _map_value(
            line_items,
            "ta",
            "travel_allowance",
            "travelling_allowance",
            "transport_allowance",
        )

        medical = _map_value(
            line_items,
            "medical",
            "medical_allowance",
        )

        ta_da = _map_value(
            line_items,
            "ta_da",
            "da_on_ta",
            "dearness_allowance_on_ta",
        )

        gross = _number(row.get("gross_salary"))

        if gross <= 0:
            gross = (
                basic
                + da
                + hra
                + _regular_component_extras(line_items)
            )

        arrear_gross = _arrear_gross_from_flags(flags)

        if arrear_gross <= 0 and source == "arrear":
            arrear_gross = gross

        if arrear_gross <= 0:
            arrear_gross = _arrear_component_total(line_items)

        arrear_gross = min(
            max(arrear_gross, 0.0),
            max(gross, 0.0),
        )

        arrear_da = _arrear_da_total(line_items)

        gpf = _deduction_total_for_head(
            deductions,
            "gpf",
        )

        gli = _deduction_total_for_head(
            deductions,
            "gli",
            "gis",
        )

        prof_tax = _deduction_total_for_head(
            deductions,
            "professional_tax",
            "prof_tax",
        )

        tds = _deduction_total_for_head(
            deductions,
            "income_tax_tds",
            "tds",
            "tax",
        )

        total_deduction = _sum_numeric_map(deductions)
        net_pay = gross - total_deduction

        month_name = MONTH_NAMES.get(
            month,
            f"Month {month}" if month else "Unknown Month",
        )

        labels = []

        if source == "combined_period":
            labels.append("Combined")

        if row.get("is_auto_generated"):
            labels.append("Auto")

        if arrear_gross > 0:
            labels.append(
                "Arrear"
                if source == "arrear"
                else "+Arrear"
            )

        if labels:
            month_name += f" ({' / '.join(labels)})"

        formatted.append(
            {
                "month": month,
                "year": year,
                "month_year": row.get("month_year"),
                "month_name": month_name,

                "basic": _money(basic),
                "da": _money(da),
                "hra": _money(hra),
                "ta": _money(ta),
                "medical": _money(medical),

                "gross": _money(gross),

                "gpf": _money(gpf),
                "gli": _money(gli),
                "gis": _money(gli),

                "prof_tax": _money(prof_tax),
                "tds": _money(tds),

                "total_deduction": _money(
                    total_deduction
                ),

                "net_pay": _money(net_pay),

                "arrear_amount": _money(
                    arrear_gross
                ),

                "source": source,
                "note": row.get("note"),
                "flags": flags,
                "is_auto_generated": bool(
                    row.get("is_auto_generated")
                ),
            }
        )

        totals["basic"] += basic
        totals["da"] += da
        totals["hra"] += hra
        totals["ta"] += ta
        totals["medical"] += medical
        totals["ta_da"] += ta_da
        totals["gross"] += gross
        totals["arrear"] += arrear_gross
        totals["arrear_da"] += arrear_da

        totals["gpf"] += gpf
        totals["gli"] += gli
        totals["prof_tax"] += prof_tax
        totals["tds"] += tds

        totals["total_deduction"] += total_deduction
        totals["net_pay"] += net_pay

    return formatted, totals


def _quarter_summary(formatted_ledger, financial_year):
    """
    Build the project's March -> February quarter totals from actual ledger rows.

    Q1 = March, April, May
    Q2 = June, July, August
    Q3 = September, October, November
    Q4 = December, January, February

    Only rows belonging to the requested FY are counted. TDS is the deduction
    already normalised by _prepare_ledger(), so regular and arrear TDS recorded
    in that payment month are included. Missing months are never invented here.
    """
    _, start_year, end_year = _validate_financial_year(financial_year)

    quarters = {
        1: {"gross": 0.0, "tds": 0.0},
        2: {"gross": 0.0, "tds": 0.0},
        3: {"gross": 0.0, "tds": 0.0},
        4: {"gross": 0.0, "tds": 0.0},
    }

    quarter_for_month = {
        3: 1, 4: 1, 5: 1,
        6: 2, 7: 2, 8: 2,
        9: 3, 10: 3, 11: 3,
        12: 4, 1: 4, 2: 4,
    }

    for row in (formatted_ledger or []):
        month = int(row.get("month", 0) or 0)
        year = int(row.get("year", 0) or 0)

        quarter = quarter_for_month.get(month)
        if not quarter:
            continue

        expected_year = start_year if month >= 3 else end_year
        if year != expected_year:
            continue

        quarters[quarter]["gross"] += _number(row.get("gross"))
        quarters[quarter]["tds"] += _number(row.get("tds"))

    return quarters


def _text_value(mapping, key, default=""):
    """Return a trimmed optional text field from a future/manual payload."""
    if not isinstance(mapping, dict):
        return default
    value = mapping.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def _normalise_bin_rows(rows):
    """
    Future-ready Book Adjustment/BIN row shape. No value is invented.

    Each row:
      tax_deposited, receipt_no_24g, ddo_serial_no_24g,
      transfer_voucher_date, match_status_24g
    """
    result = []
    for raw in (rows or []):
        if not isinstance(raw, dict):
            continue
        result.append({
            "tax_deposited": _money(raw.get("tax_deposited"))
                if raw.get("tax_deposited") not in (None, "") else "",
            "receipt_no_24g": _text_value(raw, "receipt_no_24g"),
            "ddo_serial_no_24g": _text_value(raw, "ddo_serial_no_24g"),
            "transfer_voucher_date": _text_value(raw, "transfer_voucher_date"),
            "match_status_24g": _text_value(raw, "match_status_24g"),
        })
    return result[:12]


def _normalise_challan_rows(rows):
    """
    Future-ready challan row shape. No value is invented.

    Each row:
      tax_deposited, bsr_code, transfer_voucher_date,
      challan_serial_no, oltas_status
    """
    result = []
    for raw in (rows or []):
        if not isinstance(raw, dict):
            continue
        result.append({
            "tax_deposited": _money(raw.get("tax_deposited"))
                if raw.get("tax_deposited") not in (None, "") else "",
            "bsr_code": _text_value(raw, "bsr_code"),
            "transfer_voucher_date": _text_value(raw, "transfer_voucher_date"),
            "challan_serial_no": _text_value(raw, "challan_serial_no"),
            "oltas_status": _text_value(raw, "oltas_status"),
        })
    return result[:4]


def _tax_value(tax_result, *names, default=0.0):
    for name in names:
        if name in tax_result:
            return _number(tax_result.get(name))
    return _number(default)


def _slab_band_tax(taxable_income, lower, upper):
    """
    Derive a displayed slab-band amount from the same tax-calculator function
    instead of duplicating the calculator's arithmetic.
    """
    lower_tax = compute_slab_tax(min(taxable_income, lower))
    upper_tax = compute_slab_tax(min(taxable_income, upper))
    return max(0.0, upper_tax - lower_tax)


def _integer_to_words(value):
    """
    Small dependency-free Indian-numbering helper for the TDS words field.
    """
    number = int(round(max(_number(value), 0.0)))

    if number == 0:
        return "Zero"

    ones = [
        "",
        "One",
        "Two",
        "Three",
        "Four",
        "Five",
        "Six",
        "Seven",
        "Eight",
        "Nine",
        "Ten",
        "Eleven",
        "Twelve",
        "Thirteen",
        "Fourteen",
        "Fifteen",
        "Sixteen",
        "Seventeen",
        "Eighteen",
        "Nineteen",
    ]

    tens = [
        "",
        "",
        "Twenty",
        "Thirty",
        "Forty",
        "Fifty",
        "Sixty",
        "Seventy",
        "Eighty",
        "Ninety",
    ]

    def under_hundred(n):
        if n < 20:
            return ones[n]
        return " ".join(
            part
            for part in (
                tens[n // 10],
                ones[n % 10],
            )
            if part
        )

    def under_thousand(n):
        parts = []

        if n >= 100:
            parts.extend(
                [
                    ones[n // 100],
                    "Hundred",
                ]
            )
            n %= 100

        if n:
            parts.append(under_hundred(n))

        return " ".join(parts)

    parts = []

    crore = number // 10_000_000
    number %= 10_000_000

    lakh = number // 100_000
    number %= 100_000

    thousand = number // 1_000
    number %= 1_000

    if crore:
        parts.append(
            f"{_integer_to_words(crore)} Crore"
        )

    if lakh:
        parts.append(
            f"{under_hundred(lakh)} Lakh"
        )

    if thousand:
        parts.append(
            f"{under_hundred(thousand)} Thousand"
        )

    if number:
        parts.append(
            under_thousand(number)
        )

    return " ".join(parts)


def generate_form16_pdf(
    employee,
    employer,
    ledger,
    financial_year,
    output_filename=None,
    tds_details=None,
):
    """
    Render Form 16 from the already-finalised ledger supplied by main.py.

    Responsibilities deliberately kept outside this file:
    - Jan/Feb projection -> main.py
    - Arrear payment-FY allocation -> main.py
    - Pay-matrix validation -> ledger/review flow
    - Tax slab policy -> core.tax_calculator
    """
    financial_year, _, _ = _validate_financial_year(
        financial_year
    )

    if not employee:
        raise ValueError(
            "Employee details are required."
        )

    if not employer:
        raise ValueError(
            "Employer details are required."
        )

    if not ledger:
        raise ValueError(
            "Salary ledger is empty."
        )

    assessment_year = _assessment_year(
        financial_year
    )

    salary_from, salary_to = _fy_period_dates(
        financial_year
    )

    formatted_ledger, totals = _prepare_ledger(
        ledger
    )

    tax_result = compute_annual_tax(
        totals["gross"]
    ) or {}

    standard_deduction = _tax_value(
        tax_result,
        "standard_deduction",
        default=75000,
    )

    taxable_income = _tax_value(
        tax_result,
        "taxable_income",
        default=max(
            totals["gross"] - standard_deduction,
            0,
        ),
    )

    tax_before_rebate = _tax_value(
        tax_result,
        "slab_tax",
        "tax_before_rebate",
        "income_tax_before_rebate",
        default=0,
    )

    tax_after_rebate = _tax_value(
        tax_result,
        "tax_after_rebate",
        "income_tax",
        "tax",
        default=tax_before_rebate,
    )

    if (
        "rebate_87a" in tax_result
        or "rebate" in tax_result
    ):
        rebate = _tax_value(
            tax_result,
            "rebate_87a",
            "rebate",
            default=0,
        )
    elif bool(tax_result.get("rebate_applied")):
        rebate = max(
            tax_before_rebate - tax_after_rebate,
            0,
        )
    else:
        rebate = 0.0

    cess = _tax_value(
        tax_result,
        "cess",
        "health_education_cess",
        default=0,
    )

    total_tax_payable = _tax_value(
        tax_result,
        "total_tax",
        "total_tax_payable",
        default=tax_after_rebate + cess,
    )

    marginal_relief = _tax_value(
        tax_result,
        "marginal_relief",
        default=0,
    )

    employee_name = (
        getattr(employee, "name", None)
        or ""
    )

    office_name = (
        getattr(
            employee,
            "office_school_name",
            None,
        )
        or ""
    )

    employee_pan = (
        getattr(employee, "pan", None)
        or ""
    )

    employer_name = (
        getattr(employer, "employer_name", None)
        or ""
    )

    employer_address = (
        getattr(
            employer,
            "employer_address",
            None,
        )
        or ""
    )

    employer_name_address = ", ".join(
        part
        for part in (
            employer_name,
            employer_address,
        )
        if part
    )

    officer_name = (
        getattr(
            employer,
            "officer_name",
            None,
        )
        or ""
    )

    officer_father_name = (
        getattr(
            employer,
            "officer_father_name",
            None,
        )
        or ""
    )

    ddo_designation = (
        getattr(
            employer,
            "designation",
            None,
        )
        or ""
    )

    generated_on = datetime.now().strftime(
        "%d-%m-%Y"
    )

    balance_or_refund = (
        total_tax_payable - totals["tds"]
    )

    # ---------------------------------------------------------
    # PART A: quarter-wise salary credited + TDS actually deducted
    # ---------------------------------------------------------
    quarter_summary = _quarter_summary(
        formatted_ledger,
        financial_year,
    )

    q1_gross = _money(quarter_summary[1]["gross"])
    q2_gross = _money(quarter_summary[2]["gross"])
    q3_gross = _money(quarter_summary[3]["gross"])
    q4_gross = _money(quarter_summary[4]["gross"])

    q1_tds = _money(quarter_summary[1]["tds"])
    q2_tds = _money(quarter_summary[2]["tds"])
    q3_tds = _money(quarter_summary[3]["tds"])
    q4_tds = _money(quarter_summary[4]["tds"])

    has_tds = totals["tds"] > 0

    # ---------------------------------------------------------
    # Future/manual TDS metadata hooks
    # ---------------------------------------------------------
    # These are optional on purpose. Current main.py does not need to pass
    # tds_details, so today's flow remains backward compatible. Later the
    # employee/employer form + DB can pass verified CIT/BIN/challan details
    # without another generator redesign.
    tds_details = tds_details if isinstance(tds_details, dict) else {}

    district_name = (
        _text_value(tds_details, "district_name")
        or str(getattr(employee, "district_name", None) or "").strip()
        or str(getattr(employer, "district_name", None) or "").strip()
        or str(getattr(employer, "district", None) or "").strip()
    )

    # Locked project rule: Treasury Name = District Name.
    treasury_name = district_name

    cit_tds = _text_value(tds_details, "cit_tds")
    cit_tds_address = _text_value(tds_details, "cit_tds_address")
    cit_tds_city = _text_value(tds_details, "cit_tds_city")
    cit_tds_pincode = _text_value(tds_details, "cit_tds_pincode")

    q1_receipt_no = _text_value(tds_details, "q1_receipt_no")
    q2_receipt_no = _text_value(tds_details, "q2_receipt_no")
    q3_receipt_no = _text_value(tds_details, "q3_receipt_no")
    q4_receipt_no = _text_value(tds_details, "q4_receipt_no")

    # Deducted TDS is known from salary ledger. Deposit/remittance is a
    # different fact, so keep it blank unless the user later supplies proof.
    q1_tax_deposited = _text_value(tds_details, "q1_tax_deposited")
    q2_tax_deposited = _text_value(tds_details, "q2_tax_deposited")
    q3_tax_deposited = _text_value(tds_details, "q3_tax_deposited")
    q4_tax_deposited = _text_value(tds_details, "q4_tax_deposited")
    total_tax_deposited = _text_value(tds_details, "total_tax_deposited")

    bin_rows = _normalise_bin_rows(tds_details.get("bin_rows"))
    challan_rows = _normalise_challan_rows(tds_details.get("challan_rows"))

    # Legacy single challan cell remains blank unless explicitly supplied.
    challan_tax = _text_value(tds_details, "challan_tax")

    template_tax_data = dict(tax_result)

    template_tax_data.update(
        {
            "total_basic": _money(
                totals["basic"]
            ),
            "total_da": _money(
                totals["da"]
            ),
            "total_hra": _money(
                totals["hra"]
            ),
            "total_med": _money(
                totals["medical"]
            ),
            "total_ta": _money(
                totals["ta"]
            ),
            "gross_salary": _money(
                totals["gross"]
            ),
            "total_gpf": _money(
                totals["gpf"]
            ),
            "total_gis": _money(
                totals["gli"]
            ),
            "total_prof_tax": _money(
                totals["prof_tax"]
            ),
            "total_tds": _money(
                totals["tds"]
            ),
            "total_deductions_sum": _money(
                totals["total_deduction"]
            ),
            "net_pay_sum": _money(
                totals["net_pay"]
            ),

            # Arrear is already inside the actual payment month's gross.
            # The old template's extra DA-arrear row would double count it,
            # so keep that legacy row disabled. Page 1 receives arrear_amount,
            # and the ledger month is labelled (+Arrear).
            "da_arrears": 0.0,
        }
    )

    template_vars = {
        # FY / AY and dynamic March -> February period.
        "financial_year": financial_year,
        "assessment_year": assessment_year,
        "salary_from": salary_from,
        "salary_to": salary_to,
        "period_from": salary_from,
        "period_to": salary_to,

        # Employee.
        "employee_name": employee_name,
        "pan": employee_pan,
        "office_name": office_name,

        # These fields are not present in EmployeeDetail. Passing an empty
        # value prevents sample/default personal data from leaking into PDFs.
        "father_name": "",
        "designation": "",
        "employee_reference": "",
        "employee_signature": "",

        # Employer / DDO.
        "employer_name": employer_name,
        "employer_address": employer_address,
        "employer_name_address": (
            employer_name_address
        ),
        "tan": (
            getattr(employer, "tan", None)
            or ""
        ),
        "employer_pan": (
            getattr(employer, "pan", None)
            or ""
        ),
        "officer_name": officer_name,
        "officer_father_name": (
            officer_father_name
        ),
        "ddo_designation": ddo_designation,

        # District/Treasury and future CIT(TDS) details.
        # Current DB may not have these yet; blank is safer than guessing.
        "district": district_name,
        "district_name": district_name,
        "treasury_name": treasury_name,
        "cit_tds": cit_tds,
        "cit_tds_address": cit_tds_address,
        "cit_tds_city": cit_tds_city,
        "cit_tds_pincode": cit_tds_pincode,

        # Future/manual BIN + challan row payloads. The current template keeps
        # the visible cells blank; these typed hooks are ready for later wiring.
        "bin_rows": bin_rows,
        "challan_rows": challan_rows,

        # Ledger.
        "ledger_data": formatted_ledger,

        # Salary / arrear totals.
        "salary_amount": _money(
            totals["basic"]
        ),
        "basic_amount": _money(
            totals["basic"]
        ),
        "da_amount": _money(
            totals["da"]
        ),
        "hra_amount": _money(
            totals["hra"]
        ),
        "ta_amount": _money(
            totals["ta"]
        ),
        "medical_amount": _money(
            totals["medical"]
        ),
        "ta_da_amount": _money(
            totals["ta_da"]
        ),
        "arrear_amount": _money(
            totals["arrear"]
        ),
        "gross_salary": _money(
            totals["gross"]
        ),
        "gross_total_income": _money(
            totals["gross"]
        ),
        "gross_total_income_after_sd": _money(
            taxable_income
        ),

        # Deductions.
        "total_gpf": _money(
            totals["gpf"]
        ),
        "total_gli": _money(
            totals["gli"]
        ),
        "total_gis": _money(
            totals["gli"]
        ),
        "total_prof_tax": _money(
            totals["prof_tax"]
        ),
        "total_tds": _money(
            totals["tds"]
        ),
        "total_tds_words": (
            _integer_to_words(totals["tds"])
        ),
        # Part-A quarter summary.
        "has_tds": has_tds,
        "q1_gross": q1_gross,
        "q2_gross": q2_gross,
        "q3_gross": q3_gross,
        "q4_gross": q4_gross,
        "q1_tds": q1_tds,
        "q2_tds": q2_tds,
        "q3_tds": q3_tds,
        "q4_tds": q4_tds,
        "q1_receipt_no": q1_receipt_no,
        "q2_receipt_no": q2_receipt_no,
        "q3_receipt_no": q3_receipt_no,
        "q4_receipt_no": q4_receipt_no,
        "q1_tax_deposited": q1_tax_deposited,
        "q2_tax_deposited": q2_tax_deposited,
        "q3_tax_deposited": q3_tax_deposited,
        "q4_tax_deposited": q4_tax_deposited,
        "total_tax_deposited": total_tax_deposited,
        "challan_tax": challan_tax,
        "tds_paid": _money(
            totals["tds"]
        ),
        "total_deduction": _money(
            totals["total_deduction"]
        ),
        "net_pay": _money(
            totals["net_pay"]
        ),
        "total_80c": _money(
            totals["gpf"] + totals["gli"]
        ),
        "total_80c_deductible": _money(
            totals["gpf"] + totals["gli"]
        ),

        # Tax.
        "standard_deduction": _money(
            standard_deduction
        ),
        "taxable_income": _money(
            taxable_income
        ),
        "tax_before_rebate": _money(
            tax_before_rebate
        ),
        "income_tax": _money(
            tax_after_rebate
        ),
        "rebate_87a": _money(
            rebate
        ),
        "net_tax": _money(
            tax_after_rebate
        ),
        "marginal_relief": _money(
            marginal_relief
        ),
        "cess": _money(
            cess
        ),
        "total_tax": _money(
            total_tax_payable
        ),
        "total_tax_payable": _money(
            total_tax_payable
        ),
        "relief_89": _money(0),
        "balance_or_refund": _money(
            balance_or_refund
        ),

        # Current template exposes these three slab bands.
        "slab_4_8": _money(
            _slab_band_tax(
                taxable_income,
                400000,
                800000,
            )
        ),
        "slab_8_12": _money(
            _slab_band_tax(
                taxable_income,
                800000,
                1200000,
            )
        ),
        "slab_12_16": _money(
            _slab_band_tax(
                taxable_income,
                1200000,
                1600000,
            )
        ),

        # Page-4 compatibility payload.
        "tax_data": template_tax_data,

        # Dates / signatures.
        "generated_on": generated_on,
        "signature_date": generated_on,
    }

    # The worker is long-lived, so reuse the precompiled Jinja template.
    rendered_html = _FORM16_TEMPLATE.render(template_vars)

    if output_filename is None:
        safe_employee = "".join(
            char
            if char.isalnum()
            else "_"
            for char in (
                employee_name
                or "Employee"
            )
        ).strip("_")

        output_filename = os.path.join(
            BASE_DIR,
            (
                f"Form16_{safe_employee}_"
                f"FY_{financial_year}.pdf"
            ),
        )

    output_filename = os.path.abspath(
        output_filename
    )

    output_dir = os.path.dirname(
        output_filename
    )

    if output_dir:
        os.makedirs(
            output_dir,
            exist_ok=True,
        )

    # form16_template.html already contains the complete page-size, margin,
    # typography and table CSS. Inject ONLY the font face here. This embeds a
    # Devanagari-capable font without changing the designed page geometry.
    font_config, font_stylesheets = _pdf_font_resources()

    HTML(
        string=rendered_html,
        base_url=BASE_DIR,
    ).write_pdf(
        output_filename,
        stylesheets=font_stylesheets,
        font_config=font_config,
    )

    return output_filename
