# Personal Telegram Gateway

A private, single-user Telegram web client. The browser talks only to this FastAPI backend. Telegram integration will later be provided by TDLib behind a `TelegramService` boundary.

## Current scope

This foundation provides:

- FastAPI liveness and component-status endpoints
- a minimal operator UI for app, VPN, Telegram network, and authorization readiness
- a `TelegramService` protocol with a non-networked mock implementation
- no Telegram credentials or TDLib calls yet

VPN credentials are Linux infrastructure secrets. They must never be stored in this repository or mixed into application configuration.

## VPS deployment

The current Debian 13 VPS deployment is private:

- `tg-photode` runs FastAPI on `127.0.0.1:8000`
- Nginx exposes the operator UI only on `127.0.0.1:8080`
- `tgapp` traffic enters the `tg-vpn` TUN interface and exits through localhost-only Xray
- SSH and root traffic keep the VPS public route; only SSH port 22 listens publicly

Open a private tunnel from the Mac:

```bash
ssh -N \
  -L 18080:127.0.0.1:8080 \
  -i ~/.ssh/tg_photode_vps_ed25519 \
  root@195.19.144.52
```

Then open <http://127.0.0.1:18080>.

The active TRUST VPN subscription reported an expiry date of 2026-08-11. Renew it or replace the Xray outbound before that date.

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn backend.main:app --reload
```

Open <http://127.0.0.1:8000>.

Run tests:

```bash
.venv/bin/python -m pytest
```

## Endpoints

- `GET /api/health` — process liveness
- `GET /api/status` — normalized application, VPN, Telegram network, and authorization readiness

## Next controlled step

Install TDLib behind the existing `TelegramService` boundary. Store `api_id` and `api_hash` only in the VPS environment file, then expose the TDLib authorization states for phone number, login code, and optional 2FA password through the private UI.
