# VetanKosh low-resource hardening test report — 2026-09-30

Completed locally in the build environment:
- Python compile checks: `main.py`, `models.py`, `worker.py`, `backup_to_oci.py`, `core/parser.py` PASS.
- Database migration on SQLite PASS; durable job table create/read/delete smoke PASS.
- FastAPI import PASS.
- `/` route via TestClient PASS (HTTP 200).
- `robots.txt` route PASS (HTTP 200).
- Jinja load checks PASS for upload, review, DDO, payment and admin dashboard templates.
- Missing parser/base/upload/review files found during package audit were restored into this package.
- Server-side PDF magic-byte and 15 MB upload cap added.
- Razorpay verify/webhook/manual approval paths enqueue idempotent durable generation jobs instead of rendering PDFs in request handlers.
- Download endpoints no longer initiate a heavy render; queued documents return HTTP 409 until the worker completes them.
- Worker uses a persistent DB queue, one-job processing, stale-lease recovery and bounded exponential retry.
- Email retries reuse an already-generated PDF and deduplicate successful generation-email delivery.
- OCI Object Storage backup script + daily systemd timer supplied.

Deployment-dependent tests still required before accepting real customer money:
- PostgreSQL migration/restore on the actual Oracle VM.
- Real OCI Object Storage backup + restore drill with the chosen private bucket.
- Real SMTP/Oracle Email Delivery send/receive test.
- Razorpay test-mode order, signature and webhook test using actual merchant credentials; then live-mode verification.
- HTTPS/DNS test for vetankosh.in after DNS points to the VM.
- Real-browser E2E with representative salary slips and a concurrency/load test on the actual VM.

These external tests cannot be truthfully marked PASS before the Oracle account, DNS, mail credentials and Razorpay credentials are configured.
