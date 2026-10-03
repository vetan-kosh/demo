# VetanKosh production replacement manifest

Replace these existing files:
- backend/main.py
- backend/models.py
- backend/migrate_db.py
- backend/pdf_generator.py
- backend/requirements.txt
- backend/templates/admin_dashboard.html
- backend/templates/ddo_details.html
- backend/templates/payment.html
- backend/templates/privacy.html
- backend/templates/terms.html
- backend/templates/form16_template.html

Add these new files:
- backend/templates/admin_login.html
- backend/templates/admin_otp.html
- backend/templates/contact.html
- backend/templates/refund.html
- backend/static/vetankosh-logo.png
- backend/static/favicon.png
- backend/PRODUCTION_ENV.example (reference only; never commit real secrets)

Keep all other existing project files unchanged, including core/parser and other user-flow templates.

Before production: configure PostgreSQL, SMTP/admin OTP variables, private archive path, HTTPS, and Razorpay secrets. Run `python migrate_db.py` once before starting the app.


## Low-resource production hardening
- Durable PostgreSQL/SQLite job queue (`durable_jobs`) keeps WeasyPrint out of web requests.
- Run one `python worker.py` process on Oracle Free Tier; scale workers only with CPU/RAM.
- Razorpay verify/webhook and manual approval enqueue idempotent generation jobs.
- Worker retries with exponential backoff and recovers stale processing leases after restart.
- Email retry reuses an already-generated PDF instead of rendering it again.
- Download endpoints no longer trigger CPU-heavy PDF rendering; while queued they return HTTP 409.
