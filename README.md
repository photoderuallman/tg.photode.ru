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
- a terminal client for listing chats, reading history, sending text, and watching new text
- normalized presence, typing/recording actions, and read-receipt events
- photo, video, voice-note, and video-note upload/download APIs
- Unicode and Telegram custom-emoji text-entity support

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
  -i ~/.ssh/your_vps_key \
  root@192.0.2.10
```

Then open <http://127.0.0.1:18080>.

The host and key path above are documentation placeholders. Keep real infrastructure
coordinates in protected deployment configuration outside the repository.

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

## REG.RU shared-host deployment

Extract the generated deployment archive directly into `www/photode.ru/tg`.
The root `.htaccess` declares `main.html` as the directory index, while the
`api/` directory contains the same-origin PHP relay used under restricted
mobile-network conditions.

The exact VPS-first upload order for the latest mobile UX build is in
[`docs/deploy-ux-pass-20260809.md`](docs/deploy-ux-pass-20260809.md).

## Native single-account iPhone build

The Xcode project in `ios/TGPhotode.xcodeproj` opens the VPS's already-authorized
Telegram account without a phone/code login screen. It talks only to
`https://photode.ru/tg/api/index.php`; a revocable private-device bearer is compiled from
the ignored `ios/DeviceSecrets.xcconfig`, while TDLib and VPN access remain on the VPS.

Installation and credential-rotation instructions are in
[`docs/iphone-single-device.md`](docs/iphone-single-device.md).

## Terminal messaging test

The running FastAPI service is the only process that owns the TDLib session. The CLI
talks to that private API; do not start a second TDLib process against the same database.

Run these commands on the VPS from `/opt/tg-photode`:

```bash
.venv/bin/python -m backend.cli me
.venv/bin/python -m backend.cli chats --limit 20
.venv/bin/python -m backend.cli messages CHAT_ID --limit 30
.venv/bin/python -m backend.cli watch
.venv/bin/python -m backend.cli send CHAT_ID "hello from the terminal"
.venv/bin/python -m backend.cli chat CHAT_ID
```

Use `Ctrl-C` to stop `watch`. The `send` command sends a real Telegram message; the
`chat` command loads recent history, listens only to the selected chat, and sends every
non-command line you type. Enter `/quit` to close it. From the Mac, keep the existing
SSH tunnel open and add `--url http://127.0.0.1:18080` before the command name.

## Endpoints

- `GET /api/health` — process liveness
- `GET /api/status` — normalized application, VPN, Telegram network, and authorization readiness
- `POST /api/transport/check` — queue a root-owned deep VPN/Telegram route check when the iPhone app becomes active
- `GET /api/telegram/auth` — normalized authorization state
- `POST /api/telegram/auth/phone` — submit an international phone number
- `POST /api/telegram/auth/code` — submit the current authorization code
- `POST /api/telegram/auth/password` — submit the optional two-step verification password
- `GET /api/telegram/me` — normalized authorized account identity
- `GET /api/chats` — main chat summaries
- `GET /api/chats/{chat_id}/messages` — recent text-message history
- `POST /api/chats/{chat_id}/messages` — send one plain-text message
- `POST /api/chats/{chat_id}/media` — send a photo, video, voice note, or video note
- `POST /api/chats/{chat_id}/read` — mark visible incoming messages as read
- `POST /api/chats/{chat_id}/messages/{message_id}/open` — mark media opened/listened/viewed
- `POST /api/chats/{chat_id}/actions` — publish typing, recording, upload, or cancel state
- `GET /api/users/{user_id}` — user identity and current presence
- `GET /api/files/{file_id}` — download a TDLib-managed media file
- `GET /api/emojis/custom/{custom_emoji_id}` — resolve a custom emoji asset
- `GET /api/events?chat_id=...` — optionally chat-filtered server-sent event stream
- `GET /api/events/next?active_chat_id=...&after_event_id=...` — replayable HTTPS long poll that keeps the visible chat active without filtering other chat updates
- `WS /api/events?chat_id=...` — optionally chat-filtered WebSocket event stream

The frontend integration contract and payload examples are in
[`docs/frontend-api.md`](docs/frontend-api.md).
