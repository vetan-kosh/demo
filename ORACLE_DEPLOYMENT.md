# VetanKosh — Oracle Free Tier production layout

Use Ubuntu + PostgreSQL + Nginx + systemd. Keep Razorpay/SMTP/OCI secrets only in `backend/.env` (chmod 600).

## Processes
- Web: 2 lightweight Uvicorn workers (`deploy/vetankosh-web.service`).
- Heavy work: exactly 1 durable worker (`deploy/vetankosh-worker.service`) on the free VM.
- Daily off-VM backup: `deploy/vetankosh-backup.timer` uploads PostgreSQL dump + archive tarball to a private OCI Object Storage bucket.

## First deploy
1. Install Python, PostgreSQL, `pg_dump`, Nginx, WeasyPrint OS dependencies and the Hindi/Devanagari font package. On Ubuntu/Debian:
   ```bash
   sudo apt update
   sudo apt install -y python3-venv python3-pip postgresql-client nginx \
     libpango-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libjpeg-turbo8 \
     fonts-noto-core fonts-noto-extra fontconfig
   sudo fc-cache -f -v
   fc-match "Noto Sans Devanagari"
   ```
   The PDF renderer prefers the installed Noto Sans Devanagari font, avoiding a network fetch during normal production rendering.
2. Create `/opt/vetankosh`, venv, install `backend/requirements.txt`.
3. Create PostgreSQL database/user and fill `.env` from `PRODUCTION_ENV.example`.
4. Run `python migrate_db.py` once before starting services.
5. Create a **private** OCI Object Storage bucket and configure least-privilege OCI credentials for `backup_to_oci.py`.
6. Install/enable the two services and backup timer from `deploy/`.
7. Install Nginx config, point `vetankosh.in` + `www.vetankosh.in` DNS A records to the VM public IP, then obtain free Let's Encrypt TLS.
8. Set `SITE_URL=https://vetankosh.in`, Razorpay live keys/webhook secret, and transactional email credentials only after test-mode E2E passes.
9. Razorpay webhook URL: `https://vetankosh.in/api/payment/gateway/webhook`.

## Capacity rule
Do not increase PDF worker count just because requests queue up. On minimum hardware, queue latency is safer than parallel WeasyPrint memory spikes. Scale worker count only after moving to more CPU/RAM and load testing.

## Restore drill
Provision a new VPS, restore PostgreSQL with `pg_restore`, restore the archive tarball, copy `.env`/secrets securely, run migrations, start services, then change DNS. This keeps Oracle replaceable rather than mandatory.
