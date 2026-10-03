# VetanKosh Final Security Fix Report — 2026-10-01

This build closes the authorization/privacy/dependency gaps found in the prior audit.

## Authorization / IDOR
- User identity is now issued by the server after salary-slip extraction; the browser no longer invents the canonical user ID.
- URL `user_id` values are never used to bind ownership.
- In-progress employee, ledger, arrear, progress, employer lookup/write, manual payment and Razorpay order creation require the HttpOnly workflow cookie to be bound server-side to the same user.
- Existing shared TAN/DDO records cannot be overwritten by an ordinary customer.
- New customer-submitted TAN records are unverified and private to their creator until an authenticated administrator verifies them.
- Returning-user Form-16 access continues to require OTP + signed short-lived access; payment downloads continue to require signed expiring tokens.

## Privacy
- Raw uploaded salary-slip PDF is temporary and removed in the request `finally` block.
- Browser `sessionStorage` salary payload is removed after ledger persistence before moving to DDO details.
- Privacy Notice v2.1 states the actual temporary-file/browser behavior and avoids inventing a universal legal retention period.
- Canonical ownership is server-side, not based on URL identifiers.

## Dependencies
- Direct runtime dependencies are pinned to explicit versions in `backend/requirements.txt`.
- `DEPENDENCY_POLICY.md` requires staged upgrades and vulnerability auditing.
- `__pycache__` and `.pyc` build artifacts are removed from the distribution.

## Verification performed here
- Python compile check: PASS.
- FastAPI import/route smoke test with SQLite: PASS.
- Unowned employee endpoint request: 403 PASS.
- Unowned employer lookup: 403 PASS.
- Unowned ledger write: 403 PASS.
- Verified/shared TAN overwrite is blocked; an unverified TAN draft can only be edited by its creating workflow: PASS.

## Still required on the real production VPS
No source-code audit can guarantee that a public service is impossible to hack. Before taking real customer data, verify TLS/DNS, firewall, PostgreSQL network isolation, real SMTP/Razorpay credentials, backup restore, dependency vulnerability scan, and an external authorization/upload/security test against the deployed site.
