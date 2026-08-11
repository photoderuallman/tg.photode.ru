from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


MODULE_PATH = Path(__file__).parents[1] / "ops" / "tg_vpn_failover.py"
SPEC = importlib.util.spec_from_file_location("tg_vpn_failover", MODULE_PATH)
assert SPEC and SPEC.loader
failover = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = failover
SPEC.loader.exec_module(failover)


def _profile(path: Path, *, address: str = "vpn.example", port: int = 443) -> Path:
    path.write_text(
        json.dumps(
            {
                "inbounds": [
                    {
                        "listen": "0.0.0.0",
                        "port": 1080,
                        "protocol": "socks",
                    }
                ],
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {
                            "vnext": [
                                {
                                    "address": address,
                                    "port": port,
                                    "users": [{"id": "secret-id"}],
                                }
                            ]
                        },
                    },
                    {"protocol": "freedom", "tag": "direct"},
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_normalization_is_localhost_only_and_forces_proxy(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "finland.json")

    normalized = failover.normalized_config(profile, 19082)

    assert normalized["inbounds"] == [
        {
            "listen": "127.0.0.1",
            "port": 19082,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "tag": "tg-vpn-failover-in",
        }
    ]
    assert normalized["outbounds"][0]["tag"] == "tg-vpn-out"
    assert normalized["routing"]["rules"][0]["outboundTag"] == "tg-vpn-out"
    assert normalized["outbounds"][0]["settings"]["vnext"][0]["users"][0]["id"] == "secret-id"


def test_endpoint_metadata_is_read_without_credentials(tmp_path: Path) -> None:
    profile = _profile(tmp_path / "netherlands.json", address="nl.example", port=8443)

    assert failover.endpoint_from_config(failover.load_json(profile)) == (
        "nl.example",
        8443,
    )


def test_transaction_paths_keep_json_extension() -> None:
    active = Path("/usr/local/etc/xray/config.json")

    assert failover.transaction_path(active, "candidate").name == ".config.candidate.json"
    assert failover.transaction_path(active, "rollback").name == ".config.rollback.json"


def test_runtime_config_is_readable_only_by_root_and_xray_group(
    monkeypatch, tmp_path: Path
) -> None:
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    changes: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        failover.pwd,
        "getpwnam",
        lambda user: SimpleNamespace(pw_gid=65534),
    )
    monkeypatch.setattr(
        failover.os,
        "chown",
        lambda target, uid, gid: changes.append(("chown", target, uid, gid)),
    )
    monkeypatch.setattr(
        failover.os,
        "chmod",
        lambda target, mode: changes.append(("chmod", target, mode)),
    )

    failover.secure_runtime_config(path)

    assert changes == [
        ("chown", path, 0, 65534),
        ("chmod", path, 0o640),
    ]


def test_transport_restart_orders_xray_before_sing_box(monkeypatch) -> None:
    calls: list[tuple[str, ...] | str] = []
    monkeypatch.setattr(
        failover,
        "systemctl",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        failover,
        "wait_for_production",
        lambda: calls.append("wait:xray"),
    )
    monkeypatch.setattr(
        failover,
        "wait_for_service",
        lambda service: calls.append(f"wait:{service}"),
    )

    failover.restart_transport()

    assert calls == [
        ("reset-failed", "xray", "sing-box"),
        ("restart", "xray"),
        "wait:xray",
        ("restart", "sing-box"),
        "wait:sing-box",
    ]


def test_order_prefers_icmp_then_latency_and_defers_cooldown(tmp_path: Path) -> None:
    low = tmp_path / "low.json"
    high = tmp_path / "high.json"
    no_icmp = tmp_path / "no-icmp.json"
    measurements = [
        (high, failover.LinkMeasurement("high", "h", 443, 90, 0, 80)),
        (no_icmp, failover.LinkMeasurement("no-icmp", "n", 443, None, 100, 30)),
        (low, failover.LinkMeasurement("low", "l", 443, 35, 0, 40)),
    ]

    ordered = failover.profile_order(measurements, {"low": 2000}, now=1000)

    assert [path.stem for path, _ in ordered] == ["high", "no-icmp", "low"]


def test_deep_check_runs_at_thirty_minute_boundary(monkeypatch) -> None:
    monkeypatch.setattr(failover, "DEEP_CHECK_SECONDS", 1800)

    assert failover.should_run_deep({"last_deep_check_epoch": 100}, 1899) is False
    assert failover.should_run_deep({"last_deep_check_epoch": 100}, 1900) is True


def test_link_measurement_connects_to_bounded_resolver_result(
    monkeypatch, tmp_path: Path
) -> None:
    profile = _profile(tmp_path / "poland.json", address="pl.example")
    connected: list[tuple[str, int]] = []
    monkeypatch.setattr(failover, "ping_latency", lambda host: (22.0, 0.0))
    monkeypatch.setattr(failover, "resolve_ipv4", lambda host: "192.0.2.10")
    monkeypatch.setattr(
        failover,
        "tcp_latency",
        lambda host, port: connected.append((host, port)) or 25.0,
    )

    measurement = failover.measure_link(profile)

    assert connected == [("192.0.2.10", 443)]
    assert measurement.endpoint == "pl.example"
    assert measurement.tcp_ms == 25.0


def test_data_probe_requires_majority_of_rounds(monkeypatch) -> None:
    outcomes = iter(
        [
            (True, [40.0], []),
            (False, [], ["timeout"]),
            (True, [60.0], []),
        ]
    )
    monkeypatch.setattr(failover, "one_data_round", lambda proxy: next(outcomes))

    result = failover.probe_data(("127.0.0.1", 18082), rounds=3)

    assert result.passed is True
    assert result.successful_rounds == 2
    assert result.latency_ms == 50.0


def test_data_probe_converts_whole_round_timeout_to_failure(monkeypatch) -> None:
    monkeypatch.setattr(failover, "ROUND_TIMEOUT", 0.01)
    monkeypatch.setattr(
        failover,
        "one_data_round",
        lambda proxy: (__import__("time").sleep(0.1), [], [])[0],
    )

    result = failover.probe_data(("127.0.0.1", 18082), rounds=1)

    assert result.passed is False
    assert result.failures == ["round_timeout"]


def test_service_https_probe_runs_as_tgapp_without_an_explicit_proxy(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []

    def completed(command: list[str], **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="204 0.125", stderr="")

    monkeypatch.setattr(failover.subprocess, "run", completed)

    latency = failover.service_https_probe("www.gstatic.com", 443, "/generate_204")

    assert latency == 125.0
    assert calls[0][:5] == [
        str(failover.RUNUSER_BIN),
        "-u",
        "tgapp",
        "--",
        str(failover.CURL_BIN),
    ]
    assert "--proxy" not in calls[0]
    assert calls[0][-1] == "https://www.gstatic.com/generate_204"


def test_service_route_probe_requires_telegram_and_neutral_payloads(
    monkeypatch,
) -> None:
    outcomes = {
        "api.telegram.org": OSError("offline"),
        "core.telegram.org": 80.0,
        "www.gstatic.com": 100.0,
    }

    def probe(host: str, port: int = 443, path: str = "/") -> float:
        outcome = outcomes[host]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(failover, "service_https_probe", probe)

    result = failover.probe_service_route(rounds=1)

    assert result.passed is True
    assert result.successful_rounds == 1
    assert result.latency_ms == 90.0


def test_active_check_rejects_broken_service_route(monkeypatch, tmp_path: Path) -> None:
    saved: list[dict] = []
    monkeypatch.setattr(failover, "STATE_DIR", tmp_path)
    monkeypatch.setattr(failover, "load_state", lambda: {})
    monkeypatch.setattr(failover, "save_state", lambda state: saved.append(dict(state)))
    monkeypatch.setattr(failover, "should_run_deep", lambda state, now: False)
    monkeypatch.setattr(
        failover,
        "systemctl",
        lambda *args, **kwargs: SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        failover,
        "probe_data",
        lambda proxy, rounds: failover.DataProbe(True, rounds, rounds, 50.0, []),
    )
    monkeypatch.setattr(
        failover,
        "probe_service_route",
        lambda rounds: failover.DataProbe(
            False,
            rounds,
            0,
            None,
            ["route:https:core.telegram.org:OSError"],
        ),
    )
    monkeypatch.setattr(failover, "log", lambda *args, **kwargs: None)

    result = failover.run(force_deep=False, force_failover=False, dry_run=False)

    assert result == 1
    assert saved[-1]["status"] == "route_degraded"
    assert saved[-1]["last_probe"]["passed"] is True
    assert saved[-1]["last_route_probe"]["passed"] is False
