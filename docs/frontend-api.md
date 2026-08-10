# Frontend Telegram API contract

The browser talks only to the private FastAPI service. Raw TDLib objects, file paths,
API credentials, and the encrypted session never enter the frontend.

## Selected-chat lifecycle

1. Call `GET /api/chats` and retain the selected `id` and optional `peer_user_id`.
2. Load `GET /api/chats/{chat_id}/messages?limit=30`.
3. To prepend older history, repeat the request with the oldest loaded Telegram
   message ID as an exclusive cursor:
   `GET /api/chats/{chat_id}/messages?limit=30&before_message_id={oldest_id}`.
4. Open one `WS /api/events?chat_id={chat_id}` connection. SSE at the same path is also supported.
5. When incoming messages become visible, call `POST /api/chats/{chat_id}/read`.
6. Close the event connection when leaving the chat. The backend balances TDLib `openChat`/`closeChat` automatically.

## Message payload

Every text or media message has one normalized shape:

```json
{
  "id": 123,
  "chat_id": 42,
  "sender_id": 77,
  "sender_type": "user",
  "is_outgoing": false,
  "sent_at": "2026-08-09T10:00:00Z",
  "kind": "text",
  "text": "hello 🙂",
  "entities": [],
  "media": null,
  "is_read": true,
  "sending_state": "sent"
}
```

`kind` is `text`, `photo`, `video`, `voice_note`, `video_note`, or `unsupported`.
Media messages include `media.download_url`; use that URL instead of constructing a
filesystem path. `sending_state` is `pending`, `sent`, or `failed`.

For outgoing messages, `is_read` means the other side has read the message. For incoming
messages, it means this account has marked it read. A `receipt.updated` event advances
the marker for every message whose ID is at or below `last_read_message_id`.

## Presence and chat actions

Initial presence:

```http
GET /api/users/{peer_user_id}
```

Presence states are `online`, `offline`, `recently`, `last_week`, `last_month`, and
`unknown`. Exact timestamps are nullable because Telegram privacy settings can hide them.

Send the current user's activity with:

```http
POST /api/chats/{chat_id}/actions
Content-Type: application/json

{"action":"typing","progress":0}
```

Actions include `typing`, `recording_voice_note`, `recording_video`,
`recording_video_note`, the four `uploading_*` variants, and `cancel`. Send `cancel`
when the input is cleared or recording stops.

## Read and opened state

Mark only messages actually visible in the viewport:

```http
POST /api/chats/{chat_id}/read
Content-Type: application/json

{"message_ids":[101,102,103]}
```

After the user opens a photo/video or listens to a voice/video note, call:

```http
POST /api/chats/{chat_id}/messages/{message_id}/open
```

That second call handles Telegram's listened/viewed state and self-destruct timers; it
does not replace the ordinary read call.

## Text and emoji

Ordinary emoji are plain Unicode text and require no special payload. Telegram custom
emoji use a `custom_emoji` entity:

```json
{
  "text": "🙂",
  "entities": [
    {
      "offset": 0,
      "length": 2,
      "type": "custom_emoji",
      "custom_emoji_id": 9999
    }
  ]
}
```

Entity offsets and lengths are UTF-16 code units, matching browser string indexing and
TDLib. Resolve a received custom emoji through
`GET /api/emojis/custom/{custom_emoji_id}`, then download its `download_url`. The format
is `webp`, `tgs`, `webm`, or `unknown`.

## Media and recordings

Send `multipart/form-data` to `POST /api/chats/{chat_id}/media`:

- `file`: required Blob/File
- `kind`: `photo`, `video`, `voice_note`, or `video_note`
- `caption`: optional, except Telegram video notes don't display captions
- `duration`, `width`, `height`: numeric metadata when known

Browser `MediaRecorder` WebM voice is converted to mono Opus/Ogg. Video notes are cropped
square, limited to 60 seconds, and converted to H.264/AAC MP4. Other browser videos are
converted to streamable MP4 when needed. Uploads are limited to 100 MB.

## Realtime events

The selected-chat event stream emits these `type` values:

- `message.new`, `message.sent`, `message.failed`, `message.content_updated`
- `presence.updated`
- `chat.action`
- `receipt.updated`
- `message.content_opened`

All chat-scoped events contain `chat_id`. Payload-specific data is in `message`,
`presence`, `action`, or `receipt`. Missing payload fields are omitted rather than null.
