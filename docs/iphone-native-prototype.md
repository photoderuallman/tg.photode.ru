# Legacy multi-account iPhone prototype

> This document describes the superseded phone/code login checkpoint. Production now
> uses the single-account no-login build in `docs/iphone-single-device.md`.

This build keeps the private network path explicit:

```text
iPhone -> https://photode.ru/tg/api/index.php -> VPS -> tg-vpn -> Telegram
```

The Swift source has one HTTPS relay URL. It contains no VPS address, Telegram API ID,
API hash, VPN credential, or TDLib database key. Redirects to any host other than
`photode.ru` are rejected. The Telegram bearer token is stored in the iPhone Keychain
with `ThisDeviceOnly` accessibility.

## What the prototype supports

- existing-account phone-number login
- Telegram login code
- optional Telegram two-step-verification password
- isolated TDLib database and encryption key per device login
- Keychain session restore and logout
- 12-row chat list, Saved Messages label, and the approved minimal palette/grid
- load the latest 50 messages when a chat opens
- send and receive multiline text in realtime through HTTPS long polling
- online/recent/last-seen/typing status and outgoing read-receipt updates
- Enter/Return adds a line; only `S.` sends

This native checkpoint does not yet implement the `M.` media picker or `O.` video-note
recorder. Those controls are deliberately disabled instead of pretending to send.

## 1. Enable multi-account login on the VPS

Upload the packaged backend over the existing `/opt/tg-photode` application. Preserve
the real `/etc/tg-photode/tg-photode.env`; never put that file in the project archive.

Add or change these values in `/etc/tg-photode/tg-photode.env`:

```dotenv
TELEGRAM_AUTH_MODE=tdlib
TELEGRAM_MULTI_ACCOUNT_ENABLED=true
TELEGRAM_MAX_ACCOUNT_SESSIONS=3
TELEGRAM_LOGIN_FLOW_TTL_SECONDS=600
TELEGRAM_ACCOUNT_SESSION_TTL_SECONDS=2592000
TDLIB_ACCOUNTS_DIRECTORY=/var/lib/tg-photode/accounts
TELEGRAM_ACCOUNT_TOKEN_SECRET=PASTE_A_NEW_RANDOM_SECRET_HERE
```

Generate the last value on the VPS:

```bash
openssl rand -hex 32
```

Then prepare storage and restart:

```bash
sudo install -d -o tgapp -g tgapp -m 700 /var/lib/tg-photode/accounts
sudo systemctl daemon-reload
sudo systemctl restart tg-photode
sudo systemctl status tg-photode --no-pager
curl --fail --silent http://127.0.0.1:8000/api/health
```

Keep `TELEGRAM_MAX_ACCOUNT_SESSIONS=3` on the 1 GB test VPS. Each signed-in account owns
a separate TDLib process state and database, so increasing this without watching memory
can exhaust the server.

## 2. Keep the photode.ru relay current

Upload `hosting/rg/api/index.php` as:

```text
www/photode.ru/tg/api/index.php
```

The relay forwards the `Authorization: Bearer ...` header and normal JSON POST bodies.
The iPhone never connects to the upstream VPS hostname directly.

## 3. Install from Xcode

1. Open `ios/TGPhotode.xcodeproj` in Xcode.
2. Select the `TGPhotode` target, open **Signing & Capabilities**, and choose your Apple
   development team. Change `ru.photode.telegram` only if Xcode says the identifier is
   already owned.
3. Connect the iPhone by cable or Xcode's paired wireless connection, unlock it, and
   enable Developer Mode if iOS asks.
4. Select the physical iPhone as the run destination and press **Run**.
5. On first launch, enter an existing Telegram phone number, then its code, then the
   optional two-step password.

Telegram account creation is intentionally absent. If the number has no account, create
it in the official Telegram app first.

## 4. Five-minute acceptance test

1. Sign in and confirm the main status becomes `RDY`.
2. Open a chat and confirm up to 50 recent messages appear in chronological order.
3. Press Return in the composer and confirm it inserts a new line without sending.
4. Tap `S.` and confirm the same message appears in official Telegram.
5. Reply from another Telegram client and confirm the message appears without reopening
   the chat.
6. Read the outgoing message in the other client and confirm its gray `Y.` becomes white.
7. Force-quit and reopen the app; it should restore the Keychain session without asking
   for the phone again.
8. Tap the account bowl to return to the chat list. Remove the app or use logout when the
   device should no longer retain access.

## Security boundary

The domain restriction protects the client route; it does not make a publicly reachable
login endpoint harmless. Keep TLS valid, keep the relay's upstream fixed, limit active
TDLib sessions, and never expose API credentials or database keys to the iPhone bundle.
