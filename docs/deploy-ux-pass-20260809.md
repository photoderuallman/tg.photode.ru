# Deploy the 2026-08-09 Telegram UX pass

Deploy the VPS patch first. The new browser build expects profile-photo references in
the Telegram account, chat, and user payloads; the old backend safely ignores the new UI,
but the new color bowls cannot use real profile colors until the VPS is updated.

## 1. VPS backend

Upload `TG-PHOTODE-VPS-UX-20260809.tar.gz` to `/tmp` on the VPS, then run:

```bash
cd /opt/tg-photode
sudo tar -xzf /tmp/TG-PHOTODE-VPS-UX-20260809.tar.gz -C /opt/tg-photode
sudo systemctl restart tg-photode
sudo systemctl --no-pager --full status tg-photode
curl -fsS http://127.0.0.1:8000/api/health
```

The archive contains only `backend/models.py` and `backend/telegram/tdlib.py`. It has no
Telegram API credentials, phone number, TDLib database, session, VPN configuration, or
environment file.

## 2. REG.RU `/tg`

Open `www/photode.ru/tg` in the REG.RU file manager. Extract the contents of
`TG-PHOTODE-HOST-UX-20260809.zip` directly into that directory and replace the matching
files. The archive already has the correct root layout:

```text
main.html
.htaccess
static/css/app.css
static/js/app.js
api/index.php
api/.htaccess
```

Do not create another folder inside `/tg`. After extraction, the page entry must be
`/tg/main.html`, not `/tg/TG-PHOTODE-HOST-UX-20260809/main.html`.

## 3. Phone check

Open `https://photode.ru/tg/` in a fresh private tab once so no old CSS/JavaScript cache
is involved. Check this short path:

1. Main screen says `RDY`; chats occupy successive red-grid rows with `?grid=1`.
2. “Saved Messages” has that exact title.
3. Open a chat: it slides over the list and shows up to 50 recent messages.
4. Scroll upward and wait for media/history refresh: the viewport must not move by itself.
5. Type text with Return: Return creates a new line, `O.` becomes `S.`, and no focus box appears.
6. Read and unread outgoing batches use separate white and gray `Y.` labels.
7. Hold `O.` for 0.5 seconds, allow camera and microphone once, switch cameras, then send.
8. Tap a video note for sound; it expands upward, fills the circle, and collapses after playback.

Camera and microphone permission is controlled by Safari/iOS. The site now explicitly allows
both features and reuses a stored grant, but web code cannot force a permanent browser grant.
If iOS is configured to Ask every time or site data is cleared, Safari will ask again.
