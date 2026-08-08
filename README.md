# Personal Telegram Gateway

A private, single-user Telegram web client. The browser talks only to this FastAPI backend. Telegram authorization is provided by TDLib behind a `TelegramService` boundary.

## Current scope

This foundation provides:

- FastAPI liveness and component-status endpoints
- a private operator UI for app, VPN, Telegram network, and authorization readiness
- a normalized phone → code → optional 2FA authorization API and UI
- a `TelegramService` protocol with both mock and native TDLib implementations
- persistent encrypted TDLib session storage outside the repository
- a real phone → code → optional 2FA authorization flow through the private UI

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

Run the authorization simulator with dummy values only:

```bash
TELEGRAM_AUTH_MODE=mock \
TELEGRAM_MOCK_REQUIRE_PASSWORD=true \
.venv/bin/python -m uvicorn backend.main:app --reload
```

Mock inputs are validated, passed through the same application boundary as TDLib,
and immediately discarded. They are not logged or stored by the service.

Real TDLib mode requires Debian's packaged runtime plus protected environment values:

```bash
sudo apt-get install libtdjson1.8.38
```

Set `TELEGRAM_AUTH_MODE=tdlib`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and a
random `TDLIB_DATABASE_ENCRYPTION_KEY` only in the server environment file. TDLib's
database and files directories must be writable by the application service account.

Run tests:

```bash
.venv/bin/python -m pytest
```

## Endpoints

- `GET /api/health` — process liveness
- `GET /api/status` — normalized application, VPN, Telegram network, and authorization readiness
- `GET /api/telegram/auth` — normalized authorization state
- `POST /api/telegram/auth/phone` — submit an international phone number
- `POST /api/telegram/auth/code` — submit the current authorization code
- `POST /api/telegram/auth/password` — submit the optional two-step verification password

## Next controlled step

After the private UI reaches `ready`, add chat-list and message methods to the existing
`TelegramService` boundary. Keep raw TDLib objects and commands out of browser routes.
