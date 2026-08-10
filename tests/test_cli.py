import json
from threading import Event
from typing import Any

from backend import cli


class FakeEventResponse:
    def __enter__(self) -> "FakeEventResponse":
        return self

    def __exit__(self, *args: Any) -> None:
        return None

    def __iter__(self):
        event = {
            "type": "message.new",
            "message": {
                "id": 7,
                "chat_id": 42,
                "sender_id": 1000,
                "sender_type": "user",
                "is_outgoing": False,
                "sent_at": "2026-08-08T19:00:00Z",
                "text": "selected update",
            },
        }
        yield f"data: {json.dumps(event)}\n".encode()
        yield b"\n"


def test_stream_events_requests_only_the_selected_chat(monkeypatch: Any) -> None:
    requested_urls: list[str] = []

    def fake_urlopen(request: Any) -> FakeEventResponse:
        requested_urls.append(request.full_url)
        return FakeEventResponse()

    monkeypatch.setattr(cli, "urlopen", fake_urlopen)
    events = list(cli._stream_events("http://gateway", chat_id=42))

    assert requested_urls == ["http://gateway/api/events?chat_id=42"]
    assert events[0]["message"]["chat_id"] == 42


def test_chat_command_dispatches_one_interactive_session(monkeypatch: Any) -> None:
    calls: list[tuple[str, int, int]] = []

    def fake_chat(base_url: str, chat_id: int, *, history_limit: int) -> None:
        calls.append((base_url, chat_id, history_limit))

    monkeypatch.setattr(cli, "_chat", fake_chat)

    exit_code = cli.main(
        ["--url", "http://gateway", "chat", "42", "--history", "12"]
    )

    assert exit_code == 0
    assert calls == [("http://gateway", 42, 12)]


def test_chat_transcript_uses_same_format_for_both_directions() -> None:
    incoming = {
        "sent_at": "2026-08-08T19:00:00Z",
        "is_outgoing": False,
        "text": "hello",
    }
    outgoing = {**incoming, "is_outgoing": True, "text": "hey"}

    assert "] them: hello" in cli._chat_message_line(incoming)
    assert "] you: hey" in cli._chat_message_line(outgoing)


def test_interactive_chat_mixes_history_updates_and_sent_text(
    monkeypatch: Any,
    capsys: Any,
) -> None:
    update_rendered = Event()
    sent_payloads: list[dict[str, Any]] = []
    inputs = iter(["terminal reply", "/quit"])

    def message(
        message_id: int,
        text: str,
        *,
        outgoing: bool,
    ) -> dict[str, Any]:
        return {
            "id": message_id,
            "chat_id": 42,
            "sender_id": 1000,
            "sender_type": "user",
            "is_outgoing": outgoing,
            "sent_at": "2026-08-08T19:00:00Z",
            "text": text,
        }

    def fake_request(
        base_url: str,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
    ) -> Any:
        if path.startswith("/api/chats?"):
            return [{"id": 42, "title": "Selected chat"}]
        if method == "GET":
            return [message(1, "history text", outgoing=False)]
        assert payload is not None
        sent_payloads.append(payload)
        return message(3, payload["text"], outgoing=True)

    def fake_events(base_url: str, *, chat_id: int | None = None):
        assert chat_id == 42
        yield {"type": "message.new", "message": message(2, "live text", outgoing=False)}
        update_rendered.set()

    def fake_input(prompt: str) -> str:
        update_rendered.wait(timeout=1)
        return next(inputs)

    monkeypatch.setattr(cli, "_request_json", fake_request)
    monkeypatch.setattr(cli, "_stream_events", fake_events)
    monkeypatch.setattr("builtins.input", fake_input)

    cli._chat("http://gateway", 42, history_limit=30)

    output = capsys.readouterr().out
    assert "Selected chat" in output
    assert "] them: history text" in output
    assert "] them: live text" in output
    assert "] you: terminal reply" in output
    assert sent_payloads == [{"text": "terminal reply"}]
