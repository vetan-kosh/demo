# VetanKosh pre-launch tax fix — 2026-10-03

Fixed the launch-blocking FY 2025-26 / AY 2026-27 new-regime edge case found in the final pre-launch audit.

- Implemented section 87A marginal relief for eligible resident individuals with normal slab-rate income marginally above Rs 12,00,000 taxable income.
- Preserved Rs 60,000 rebate / nil normal slab-rate tax up to Rs 12,00,000.
- Preserved 4% Health & Education Cess after rebate/marginal relief.
- Added an explicit resident-individual eligibility switch.
- Documented that special-rate income (for example certain capital gains) is outside this salary-only calculator and must be handled separately.
- Negative inputs are safely clamped to zero.

Verification after patch:
- Python compile: PASS
- SQLite migration: PASS
- Tax boundary checks at Rs 12,00,000 and just above: PASS
- Non-resident rebate guard: PASS
- FastAPI route smoke for home/privacy/terms/contact/refund/review/DDO/payment/robots: PASS

Production-dependent tests (SMTP, Razorpay, HTTPS/DNS, PostgreSQL on VPS, backup restore) remain deployment checks and are not represented as locally passed.
