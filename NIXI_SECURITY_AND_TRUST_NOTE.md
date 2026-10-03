# VetanKosh — Security & Trust Note for Domain/Partner Review

VetanKosh is an independently developed private software platform for salary-slip processing, salary/tax data review and Form-16 preparation/document workflow. It is not a Government website, Income Tax Department/TRACES portal, NIXI service, or Government-authorised portal, and does not claim such status.

The service is designed to protect user information through layered safeguards including HTTPS deployment, restrictive browser security headers, controlled PDF uploads, authenticated/OTP-protected administrative access, CSRF protection, brute-force and API abuse throttling, expiring signed access tokens for sensitive customer downloads, authorization checks for archived Form-16 documents, payment-signature and webhook verification, private document storage, database-backed audit/state records, background job isolation, and private off-server backup capability.

VetanKosh does not intentionally store card/UPI credentials. Online payment credentials are handled by the configured payment provider; VetanKosh verifies provider signatures and stores only the application/payment records required for the workflow.

Privacy, Terms, Refund/Correction and Contact pages are included. A user who believes a delivered report is incorrect may request administrative correction/review or request an eligible refund review according to the published policy.

Security is treated as an ongoing process. The operator should keep TLS certificates, operating-system packages, application dependencies and secrets maintained; restrict database/server access; test backups; monitor failures and suspicious activity; and perform security testing before public launch and after material changes.

This note describes technical safeguards in the supplied VetanKosh build. It is not a claim of Government affiliation, certification, endorsement, or immunity from cyberattack.
