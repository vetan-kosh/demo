"""
New Tax Regime calculator for FY 2025-26 / AY 2026-27.

Salary-workflow scope: normal slab-rate income only. Income taxable at special
rates (for example certain capital gains) must be handled separately and is not
eligible for the section 87A rebate in the same way.
"""

STANDARD_DEDUCTION = 75000
REBATE_87A_LIMIT = 1200000
REBATE_87A_MAX = 60000
CESS_RATE = 0.04

DEFAULT_SLABS = [
    (400000, 0.00),
    (800000, 0.05),
    (1200000, 0.10),
    (1600000, 0.15),
    (2000000, 0.20),
    (2400000, 0.25),
    (float("inf"), 0.30),
]


def compute_slab_tax(taxable_income: float, slabs=DEFAULT_SLABS) -> float:
    taxable_income = max(0.0, float(taxable_income or 0))
    tax = 0.0
    prev_limit = 0.0
    for limit, rate in slabs:
        if taxable_income <= prev_limit:
            break
        slab_amount = min(taxable_income, limit) - prev_limit
        tax += slab_amount * rate
        prev_limit = limit
    return tax


def compute_annual_tax(
    annual_gross: float,
    standard_deduction: float = STANDARD_DEDUCTION,
    rebate_limit: float = REBATE_87A_LIMIT,
    slabs=DEFAULT_SLABS,
    cess_rate: float = CESS_RATE,
    resident_individual: bool = True,
) -> dict:
    """Return salary-workflow tax breakdown for the new regime.

    For an eligible resident individual, section 87A makes normal slab-rate tax
    nil up to total/taxable income of Rs 12 lakh (within this calculator's
    salary-only scope).  For income marginally above Rs 12 lakh, marginal
    relief caps pre-cess tax at the amount by which income exceeds Rs 12 lakh.

    This calculator intentionally does not mix special-rate income (e.g. some
    capital gains) into the rebate calculation; callers must handle that case
    separately.
    """
    annual_gross = max(0.0, float(annual_gross or 0))
    standard_deduction = max(0.0, float(standard_deduction or 0))
    taxable_income = max(0.0, annual_gross - standard_deduction)
    slab_tax = compute_slab_tax(taxable_income, slabs)

    rebate_applied = False
    marginal_relief = 0.0
    tax_after_rebate = slab_tax

    if resident_individual and taxable_income <= rebate_limit:
        # Under the configured AY 2026-27 slabs, slab tax at Rs 12 lakh is
        # Rs 60,000, i.e. within the section 87A maximum rebate.
        rebate_applied = True
        tax_after_rebate = max(0.0, slab_tax - min(slab_tax, REBATE_87A_MAX))
    elif resident_individual and taxable_income > rebate_limit:
        # Section 87A marginal relief: tax on income just above the rebate
        # threshold must not exceed the amount of income above that threshold.
        excess_income = taxable_income - rebate_limit
        if slab_tax > excess_income:
            marginal_relief = slab_tax - excess_income
            tax_after_rebate = excess_income

    cess = tax_after_rebate * cess_rate
    total_tax_payable = round(tax_after_rebate + cess)

    return {
        "taxable_income": taxable_income,
        "slab_tax": round(slab_tax),
        "rebate_applied": rebate_applied,
        "marginal_relief": round(marginal_relief),
        "tax_after_rebate": round(tax_after_rebate),
        "cess": round(cess),
        "total_tax_payable": total_tax_payable,
    }
