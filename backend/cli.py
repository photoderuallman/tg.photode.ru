from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from threading import Event, Lock, Thread
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import readline
except ImportError:  # pragma: no cover - available on the deployed Linux host
    readline = None


class CLIError(RuntimeError):
    """A concise error that is safe to show in the terminal."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.cli",
        description="Terminal client for the private Telegram gateway API.",
    )
    parser.add_argument(
        "--url",
        default=os.getenv("TG_GATEWAY_URL", "http://127.0.0.1:8000"),
        help="Gateway base URL (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("me", help="Show the authorized Telegram account.")

    chats = commands.add_parser("chats", help="List main Telegram chats.")
    chats.add_argument("--limit", type=int, default=20, choices=range(1, 101))

    messages = commands.add_parser("messages", help="Show recent text messages.")
    messages.add_argument("chat_id", type=int)
    messages.add_argument("--limit", type=int, default=30, choices=range(1, 101))

    send = commands.add_parser("send", help="Send one plain-text message.")
    send.add_argument("chat_id", type=int)
    send.add_argument("text", nargs="+", help="Message text (quote it if needed).")

    commands.add_parser("watch", help="Print new text messages until Ctrl-C.")

    chat = commands.add_parser(
        "chat",
        help="Open one interactive chat for history, sending, and live updates.",
    )
    chat.add_argument("chat_id", type=int)
    chat.add_argument("--history", type=int, default=30, choices=range(1, 101))
    return parser


def _request_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response)
    except HTTPError as error:
        raise CLIError(_http_error_message(error)) from None
    except URLError as error:
        raise CLIError(f"Gateway connection failed: {error.reason}") from None


def _http_error_message(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode())
        detail = payload.get("detail", payload)
        if isinstance(detail, dict):
            message = detail.get("message") or detail.get("code")
            if message:
                return f"Gateway returned HTTP {error.code}: {message}"
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    return f"Gateway returned HTTP {error.code}."


def _print_profile(profile: dict[str, Any]) -> None:
    username = f"@{profile['username']}" if profile.get("username") else "—"
    print(f"{profile['display_name']}\t{username}\tid={profile['id']}")


def _print_chats(chats: list[dict[str, Any]]) -> None:
    if not chats:
        print("No chats returned.")
        return
    print("CHAT_ID\tUNREAD\tTYPE\tTITLE\tLAST TEXT")
    for chat in chats:
        title = str(chat["title"]).replace("\t", " ").replace("\n", " ")
        last_message = str(chat.get("last_message") or "—")
        last_message = last_message.replace("\t", " ").replace("\n", " ")
        print(
            f"{chat['id']}\t{chat['unread_count']}\t{chat['type']}\t"
            f"{title}\t{last_message}"
        )


def _message_line(message: dict[str, Any]) -> str:
    direction = "→" if message["is_outgoing"] else "←"
    sender = message.get("sender_id") or "unknown"
    raw_timestamp = str(message["sent_at"])
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        time_label = timestamp.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        time_label = raw_timestamp
    text = str(message["text"]).replace("\r", "")
    return f"{time_label} {direction} sender={sender} message={message['id']}\n{text}"


def _chat_message_line(message: dict[str, Any]) -> str:
    raw_timestamp = str(message["sent_at"])
    try:
        timestamp = datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00"))
        time_label = timestamp.astimezone().strftime("%H:%M")
    except ValueError:
        time_label = raw_timestamp
    author = "you" if message["is_outgoing"] else "them"
    media_labels = {
        "photo": "[photo]",
        "video": "[video]",
        "voice_note": "[voice message]",
        "video_note": "[video message]",
        "unsupported": "[unsupported message]",
    }
    text = str(message.get("text") or media_labels.get(message.get("kind"), ""))
    text = text.replace("\r", "")
    if message.get("is_outgoing"):
        if message.get("sending_state") == "failed":
            text = f"{text}  !"
        elif message.get("sending_state") == "pending":
            text = f"{text}  …"
        else:
            text = f"{text}  {'✓✓' if message.get('is_read') else '✓'}"
    continuation = "\n" + (" " * (len(time_label) + len(author) + 5))
    return f"[{time_label}] {author}: {text.replace(chr(10), continuation)}"


def _print_messages(messages: list[dict[str, Any]]) -> None:
    if not messages:
        print("No recent text messages returned.")
        return
    for index, message in enumerate(reversed(messages)):
        if index:
            print()
        print(_message_line(message))


def _stream_events(
    base_url: str,
    *,
    chat_id: int | None = None,
):
    path = "/api/events"
    if chat_id is not None:
        path = f"{path}?{urlencode({'chat_id': chat_id})}"
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        headers={"Accept": "text/event-stream"},
    )
    try:
        with urlopen(request) as response:
            for raw_line in response:
                line = raw_line.decode().strip()
                if not line.startswith("data:"):
                    continue
                event = json.loads(line.removeprefix("data:").strip())
                if event.get("type") == "message.new":
                    yield event
    except HTTPError as error:
        raise CLIError(_http_error_message(error)) from None
    except URLError as error:
        raise CLIError(f"Gateway connection failed: {error.reason}") from None


def _watch(base_url: str) -> None:
    print("Watching new text messages. Press Ctrl-C to stop.", flush=True)
    for event in _stream_events(base_url):
        print(f"\n{_message_line(event['message'])}", flush=True)


def _chat(base_url: str, chat_id: int, *, history_limit: int) -> None:
    chats_query = urlencode({"limit": 100})
    chats = _request_json(base_url, f"/api/chats?{chats_query}")
    title = next(
        (str(chat["title"]) for chat in chats if int(chat["id"]) == chat_id),
        f"Chat {chat_id}",
    )
    history_query = urlencode({"limit": history_limit})
    history = _request_json(
        base_url,
        f"/api/chats/{chat_id}/messages?{history_query}",
    )

    print(f"=== {title} · {chat_id} ===")
    for message in reversed(history):
        print(_chat_message_line(message))
    print("Live updates are filtered to this chat. Type /quit to close.")

    seen_message_ids = {int(message["id"]) for message in history}
    seen_lock = Lock()
    output_lock = Lock()
    stop = Event()
    prompt_active = Event()

    def render(message: dict[str, Any]) -> None:
        message_id = int(message["id"])
        with seen_lock:
            if message_id in seen_message_ids:
                return
            seen_message_ids.add(message_id)

        with output_lock:
            restore_prompt = prompt_active.is_set() and sys.stdout.isatty()
            current_input = ""
            if restore_prompt and readline is not None:
                current_input = readline.get_line_buffer()
            if restore_prompt:
                sys.stdout.write("\r\033[2K")
            print(_chat_message_line(message), flush=True)
            if restore_prompt:
                sys.stdout.write(f"> {current_input}")
                sys.stdout.flush()

    def receive_updates() -> None:
        reconnect_announced = False
        while not stop.is_set():
            try:
                for event in _stream_events(base_url, chat_id=chat_id):
                    if stop.is_set():
                        return
                    reconnect_announced = False
                    render(event["message"])
            except CLIError:
                if stop.is_set():
                    return

            if not reconnect_announced:
                print("\nLive connection lost; retrying every 2 seconds.")
                reconnect_announced = True
            stop.wait(timeout=2)

    receiver = Thread(target=receive_updates, name="telegram-chat-events", daemon=True)
    receiver.start()

    try:
        while True:
            prompt_active.set()
            try:
                text = input("> ")
            except EOFError:
                break
            finally:
                prompt_active.clear()

            normalized = text.strip()
            if normalized in {"/quit", "/exit"}:
                break
            if not normalized:
                continue
            message = _request_json(
                base_url,
                f"/api/chats/{chat_id}/messages",
                method="POST",
                payload={"text": normalized},
            )
            render(message)
    finally:
        stop.set()
        print("Chat closed.")


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "me":
            _print_profile(_request_json(arguments.url, "/api/telegram/me"))
        elif arguments.command == "chats":
            query = urlencode({"limit": arguments.limit})
            _print_chats(_request_json(arguments.url, f"/api/chats?{query}"))
        elif arguments.command == "messages":
            query = urlencode({"limit": arguments.limit})
            messages = _request_json(
                arguments.url,
                f"/api/chats/{arguments.chat_id}/messages?{query}",
            )
            _print_messages(messages)
        elif arguments.command == "send":
            message = _request_json(
                arguments.url,
                f"/api/chats/{arguments.chat_id}/messages",
                method="POST",
                payload={"text": " ".join(arguments.text)},
            )
            print("Sent:")
            print(_message_line(message))
        elif arguments.command == "watch":
            _watch(arguments.url)
        elif arguments.command == "chat":
            _chat(arguments.url, arguments.chat_id, history_limit=arguments.history)
        return 0
    except CLIError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
