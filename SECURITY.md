# VetanKosh Security Architecture

VetanKosh handles salary, PAN/TAN, payment references and generated Form-16 workflow data. The application is designed with layered controls. These controls reduce risk; they are not a claim that any internet service is impossible to compromise.

## Application controls
- Production API documentation/OpenAPI endpoints are disabled.
- Security headers: HSTS on HTTPS, X-Content-Type-Options, anti-framing, Referrer-Policy, Permissions-Policy, CSP, no-store on admin/profile/download routes.
- Admin login requires configured credentials plus email OTP. OTPs are HMAC-hashed, expire, are single-use, and have an attempt cap.
- Admin sessions use high-entropy bearer tokens; only HMAC hashes are stored. Sessions expire, can be revoked, and active-session count is limited.
- Admin state-changing routes use CSRF protection.
- Login brute-force throttling and public sensitive-endpoint abuse throttling are present; Nginx adds an independent edge rate-limit layer.
- Returning-user archive access requires OTP verification and a short-lived signed access token.
- Direct admin Form-16 generation downloads require an authenticated admin session.
- Payment status and customer Form-16 download URLs require a signed, expiring payment-scoped access token; a guessed/leaked payment UUID alone is insufficient.
- Razorpay browser verification uses HMAC signature verification. Webhooks require a separate webhook secret, verify the raw-body signature, and deduplicate event IDs.
- PDF uploads are restricted to .pdf, 15 MB, PDF magic header, temporary private processing, cleanup, and a page-count cap. Scanned/unsupported PDFs fail closed rather than invoking OCR.
- Generated Form-16 files are kept outside the public static directory; archive access is mediated by application authorization.
- SQLAlchemy ORM is used for database operations; secrets belong in environment variables, not source code.
- Durable jobs have deduplication/retry/recovery behavior so a web request does not directly run the heavy PDF workload.

## Server controls to enable at deployment
- HTTPS only, TLS 1.2/1.3, HSTS.
- Uvicorn/FastAPI bound to localhost behind Nginx; PostgreSQL not exposed publicly.
- Firewall allow only SSH and HTTP/HTTPS; SSH keys, no root/password login where operationally possible.
- Run VetanKosh under a dedicated non-root OS account and restrict `.env`, archive, backup and OCI credential permissions.
- Install OS security updates, Noto Sans Devanagari, and maintain off-VM encrypted/private backups with restore tests.
- Keep production secrets unique: admin password, admin-session HMAC secret, payment-access HMAC secret, SMTP credential, Razorpay key secret, Razorpay webhook secret, backup credentials.
- Rotate any secret immediately if exposed and revoke affected sessions/keys.

## Before public launch
Run dependency vulnerability scanning, endpoint authorization tests (especially IDOR), upload fuzz/abuse tests, payment replay tests, backup restore test, and an external vulnerability scan. Re-run after material releases.

## 2026-10-01 final authorization hardening
Canonical workflow user IDs are server-issued after extraction. URL IDs are not ownership credentials. Sensitive in-progress APIs enforce server-side journey ownership using an HttpOnly cookie. Shared TAN/DDO records are protected from customer overwrite; newly submitted TAN records remain unverified/private to their creator until admin verification. See `FINAL_SECURITY_FIX_REPORT.md`.
