# VetanKosh Python optimization report — 2026-10-01

## Fixed completeness issue
- Restored `backend/core/tax_calculator.py`, which `pdf_generator.py` imports but the previous ZIP omitted.

## Hot-path optimizations
- Jinja environment and Form-16 template are created once per worker process, not once per PDF.
- WeasyPrint font configuration/CSS resources are cached once per worker process.
- Devanagari font discovery/download attempt is cached once per worker process. A temporary network/DNS failure can no longer add a fresh font-download timeout to every PDF job.
- Existing durable one-job worker, retry/backoff, stale-lease recovery, idempotent generation, and generated-PDF reuse are preserved.

## Local synthetic Form-16 benchmark
Environment here is not the target 1-vCPU/2-GB VPS, so these numbers are comparative, not a production capacity guarantee.

Same 12-month synthetic ledger and same Form-16 template:
- Previous renderer (after restoring its missing tax module): 8-run average 2.941 s; warm average 3.220 s. Repeated unavailable-font network attempts caused large ~6 s spikes.
- Optimized renderer: 8-run average 0.993 s; median 0.880 s; warm average 0.941 s.
- Observed local warm-path improvement: about 3.4x in this failure-mode benchmark.

The dominant remaining work is WeasyPrint HTML/CSS layout + PDF rendering. Python tax arithmetic/Jinja setup is not the main remaining bottleneck.

## Production notes
- Install a Devanagari-capable font at image/VM provisioning time. Do not rely on a runtime internet download for correctness.
- Keep one PDF worker on a 1-vCPU server. More CPU-bound workers on one vCPU generally contend for the same core; scale worker count with vCPU/RAM.
- Run `python benchmark_pdf.py -n 10` on the actual VPS before quoting capacity.
- Run representative real salary-slip parsing benchmarks separately; this package does not contain representative customer salary-slip PDFs.
- Tax policy remains a correctness-sensitive module. Validate slab/rebate/marginal-relief behavior against the applicable financial year before accepting real payments.

## 1 October 2026 production-policy update
- Added OS-installed Noto Sans Devanagari preference for deterministic Hindi rendering; deployment instructions install Noto packages and refresh fontconfig.
- Replaced placeholder Privacy Notice with a detailed service-specific notice covering collected data, purposes, payments, security, retention, user choices, children/guardian considerations, and independent-service status.
- Added Correction & Refund workflow: after a generated report, a user can request either admin-assisted correction or a refund review. Requests require the payment ID plus the account email, are stored in `refund_requests`, deduplicated while active, and notify the configured admin email.
- Refunds are deliberately not automatic: admin review is required before any money movement. This prevents a public endpoint from directly issuing refunds.

## Security hardening final pass (2026-10-01)
Added production docs disabling, browser security headers/CSP/no-store, public sensitive-endpoint throttling, signed expiring payment status/download tokens, admin-only direct generation downloads, PDF page-count DoS guard, generic parser error responses, hardened Nginx TLS/rate-limit template, separate payment HMAC secret, security architecture documentation and secure deployment checklist.
