# Secure deployment checklist

1. Create a dedicated non-root `koshtax` OS user; app under `/opt/vetankosh`.
2. Generate independent random secrets for ADMIN_PASSWORD, ADMIN_SESSION_SECRET, PAYMENT_ACCESS_SECRET, RAZORPAY_WEBHOOK_SECRET and other credentials. Never commit `.env`.
3. `chmod 600` the production environment file and OCI credentials; restrict Form-16 archive/backup directories to the service account.
4. Bind Uvicorn to `127.0.0.1:8000`; do not expose port 8000 publicly.
5. PostgreSQL listens only on localhost/private interface and uses a dedicated least-privilege database user.
6. Firewall: allow SSH (preferably restricted source), 80 and 443 only; deny database/app ports from the Internet.
7. Obtain a valid certificate, then use `nginx-vetankosh-hardened.conf`. Test HTTP->HTTPS redirect and headers.
8. Disable SSH root login and password authentication after confirming key login works.
9. Install Noto Sans Devanagari and current OS security updates.
10. Configure private off-VM backups and perform a restore test before launch.
11. Run the included smoke/benchmark tests plus authorization/IDOR, OTP brute-force, CSRF, upload, webhook replay and payment-signature tests.
12. Run a dependency vulnerability scanner and an external vulnerability scan before launch. Fix high/critical findings before taking real user data.
