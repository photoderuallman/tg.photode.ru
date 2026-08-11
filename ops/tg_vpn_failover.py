#!/usr/bin/env python3
"""Quality-aware, rollback-safe Xray profile selection for tg.photode.ru.

The controller never treats ICMP or a successful TCP connect as proof that a VPN
works.  Candidate profiles are ranked by link latency, started on a temporary
localhost SOCKS port, and required to carry real TLS plus Telegram DC traffic
before they can replace the production Xray configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pwd
import re
import signal
import shutil
import socket
import statistics
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator


XRAY_BIN = Path(os.getenv("TG_VPN_XRAY_BIN", "/usr/local/bin/xray"))
SYSTEMCTL_BIN = Path(os.getenv("TG_VPN_SYSTEMCTL_BIN", "/usr/bin/systemctl"))
PING_BIN = Path(os.getenv("TG_VPN_PING_BIN", "/usr/bin/ping"))
CURL_BIN = Path(os.getenv("TG_VPN_CURL_BIN", "/usr/bin/curl"))
RUNUSER_BIN = Path(os.getenv("TG_VPN_RUNUSER_BIN", "/usr/sbin/runuser"))
GETENT_BIN = Path(os.getenv("TG_VPN_GETENT_BIN", "/usr/bin/getent"))
XRAY_USER = os.getenv("TG_VPN_XRAY_USER", "nobody")
SERVICE_USER = os.getenv("TG_VPN_SERVICE_USER", "tgapp")
PROFILE_DIR = Path(os.getenv("TG_VPN_PROFILE_DIR", "/etc/tg-vpn/profiles.d"))
ACTIVE_CONFIG = Path(
    os.getenv("TG_VPN_ACTIVE_CONFIG", "/usr/local/etc/xray/config.json")
)
STATE_DIR = Path(os.getenv("TG_VPN_STATE_DIR", "/var/lib/tg-vpn-failover"))
STATE_FILE = STATE_DIR / "status.json"
BACKUP_DIR = STATE_DIR / "backups"
LOCK_FILE = Path(
    os.getenv("TG_VPN_LOCK_FILE", "/run/tg-vpn-failover/controller.lock")
)

PRODUCTION_SOCKS = ("127.0.0.1", 18082)
PROBE_PORT_START = 19082
DEEP_CHECK_SECONDS = int(os.getenv("TG_VPN_DEEP_CHECK_SECONDS", "1800"))
QUICK_FAILURE_LIMIT = int(os.getenv("TG_VPN_QUICK_FAILURE_LIMIT", "2"))
COOLDOWN_SECONDS = int(os.getenv("TG_VPN_COOLDOWN_SECONDS", "21600"))
SOCKET_TIMEOUT = float(os.getenv("TG_VPN_SOCKET_TIMEOUT", "5"))
ROUND_TIMEOUT = float(os.getenv("TG_VPN_ROUND_TIMEOUT", "20"))
ROUTE_RECOVERY_COOLDOWN_SECONDS = int(
    os.getenv("TG_VPN_ROUTE_RECOVERY_COOLDOWN_SECONDS", "600")
)

TELEGRAM_HTTPS_TARGETS = (("api.telegram.org", 443), ("core.telegram.org", 443))
TELEGRAM_DC_TARGETS = (("149.154.167.51", 443), ("149.154.167.91", 443))
NEUTRAL_HTTPS_TARGET = ("www.gstatic.com", 443, "/generate_204")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def log(event: str, **fields: object) -> None:
    payload = {"time": utc_now(), "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


@dataclass(slots=True)
class LinkMeasurement:
    profile: str
    endpoint: str
    port: int
    ping_ms: float | None
    packet_loss: float
    tcp_ms: float | None

    @property
    def order_score(self) -> float:
        if self.ping_ms is not None:
            return self.ping_ms + self.packet_loss * 10
        if self.tcp_ms is not None:
            return 10_000 + self.tcp_ms
        return float("inf")


@dataclass(slots=True)
class DataProbe:
    passed: bool
    rounds: int
    successful_rounds: int
    latency_ms: float | None
    failures: list[str] = field(default_factory=list)


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def atomic_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def load_state() -> dict[str, Any]:
    try:
        return load_json(STATE_FILE)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    atomic_json(STATE_FILE, state)


def config_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def primary_outbound(config: dict[str, Any]) -> dict[str, Any]:
    ignored = {"freedom", "blackhole", "dns", "direct", "block"}
    for outbound in config.get("outbounds", []):
        if isinstance(outbound, dict) and outbound.get("protocol") not in ignored:
            return outbound
    raise ValueError("profile has no proxy outbound")


def endpoint_from_config(config: dict[str, Any]) -> tuple[str, int]:
    outbound = primary_outbound(config)
    settings = outbound.get("settings") or {}
    options = settings.get("vnext") or settings.get("servers") or []
    if not options or not isinstance(options[0], dict):
        raise ValueError("profile proxy outbound has no server endpoint")
    host = str(options[0].get("address") or "").strip()
    port = int(options[0].get("port") or 0)
    if not host or not 1 <= port <= 65535:
        raise ValueError("profile proxy endpoint is invalid")
    return host, port


def normalized_config(path: Path, socks_port: int) -> dict[str, Any]:
    config = load_json(path)
    outbound = primary_outbound(config)
    tag = str(outbound.get("tag") or "tg-vpn-out")
    outbound["tag"] = tag
    config["inbounds"] = [
        {
            "listen": "127.0.0.1",
            "port": socks_port,
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": False},
            "tag": "tg-vpn-failover-in",
        }
    ]
    config["routing"] = {
        "domainStrategy": "AsIs",
        "rules": [
            {
                "type": "field",
                "inboundTag": ["tg-vpn-failover-in"],
                "outboundTag": tag,
            }
        ],
    }
    config["log"] = {"loglevel": "warning"}
    return config


def ping_latency(host: str) -> tuple[float | None, float]:
    try:
        completed = subprocess.run(
            [str(PING_BIN), "-n", "-c", "3", "-W", "2", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, 100.0
    output = completed.stdout + completed.stderr
    loss_match = re.search(r"([0-9.]+)% packet loss", output)
    loss = float(loss_match.group(1)) if loss_match else 100.0
    timing_match = re.search(
        r"(?:rtt|round-trip) min/avg/max/(?:mdev|stddev) = "
        r"[0-9.]+/([0-9.]+)/",
        output,
    )
    return (float(timing_match.group(1)) if timing_match else None), loss


def tcp_latency(host: str, port: int, attempts: int = 3) -> float | None:
    samples: list[float] = []
    for _ in range(attempts):
        started = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=SOCKET_TIMEOUT):
                samples.append((time.monotonic() - started) * 1000)
        except OSError:
            continue
    return statistics.median(samples) if samples else None


def resolve_ipv4(host: str) -> str | None:
    """Resolve outside the controller process so DNS has a hard deadline."""

    try:
        completed = subprocess.run(
            [str(GETENT_BIN), "ahostsv4", host],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in completed.stdout.splitlines():
        candidate = line.split(maxsplit=1)[0] if line.strip() else ""
        try:
            socket.inet_aton(candidate)
        except OSError:
            continue
        return candidate
    return None


def measure_link(path: Path) -> LinkMeasurement:
    host, port = endpoint_from_config(load_json(path))
    ping_ms, loss = ping_latency(host)
    resolved = resolve_ipv4(host)
    tcp_ms = tcp_latency(resolved, port) if resolved else None
    measurement = LinkMeasurement(path.stem, host, port, ping_ms, loss, tcp_ms)
    log("candidate_link", **asdict(measurement), order_score=measurement.order_score)
    return measurement


def recv_exact(connection: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = connection.recv(size - len(chunks))
        if not chunk:
            raise OSError("SOCKS proxy closed the connection")
        chunks.extend(chunk)
    return bytes(chunks)


def socks_connection(
    proxy: tuple[str, int], target: tuple[str, int]
) -> tuple[socket.socket, float]:
    started = time.monotonic()
    connection = socket.create_connection(proxy, timeout=SOCKET_TIMEOUT)
    connection.settimeout(SOCKET_TIMEOUT)
    try:
        connection.sendall(b"\x05\x01\x00")
        if recv_exact(connection, 2) != b"\x05\x00":
            raise OSError("SOCKS proxy rejected no-auth mode")
        host, port = target
        try:
            address = b"\x01" + socket.inet_aton(host)
        except OSError:
            encoded = host.encode("idna")
            if len(encoded) > 255:
                raise OSError("SOCKS target name is too long") from None
            address = b"\x03" + bytes([len(encoded)]) + encoded
        connection.sendall(b"\x05\x01\x00" + address + struct.pack("!H", port))
        header = recv_exact(connection, 4)
        if header[0] != 5 or header[1] != 0:
            raise OSError(f"SOCKS connect failed with code {header[1]}")
        address_size = {1: 4, 4: 16}.get(header[3])
        if header[3] == 3:
            address_size = recv_exact(connection, 1)[0]
        if address_size is None:
            raise OSError("SOCKS proxy returned an invalid address type")
        recv_exact(connection, address_size + 2)
        return connection, (time.monotonic() - started) * 1000
    except Exception:
        connection.close()
        raise


def https_probe(
    proxy: tuple[str, int], host: str, port: int = 443, path: str = "/"
) -> float:
    authority = host if port == 443 else f"{host}:{port}"
    url = f"https://{authority}{path}"
    proxy_url = f"socks5h://{proxy[0]}:{proxy[1]}"
    try:
        completed = subprocess.run(
            [
                str(CURL_BIN),
                "--silent",
                "--show-error",
                "--head",
                "--proxy",
                proxy_url,
                "--connect-timeout",
                str(SOCKET_TIMEOUT),
                "--max-time",
                str(SOCKET_TIMEOUT + 2),
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code} %{time_total}",
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=SOCKET_TIMEOUT + 4,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OSError("HTTPS probe process timed out") from error
    fields = completed.stdout.strip().split()
    if completed.returncode != 0 or len(fields) != 2 or not fields[0].startswith(("2", "3", "4")):
        raise OSError(f"HTTPS payload check failed with curl exit {completed.returncode}")
    return float(fields[1]) * 1000


def service_https_probe(host: str, port: int = 443, path: str = "/") -> float:
    """Probe through the exact service-account route used by TDLib."""

    authority = host if port == 443 else f"{host}:{port}"
    url = f"https://{authority}{path}"
    try:
        completed = subprocess.run(
            [
                str(RUNUSER_BIN),
                "-u",
                SERVICE_USER,
                "--",
                str(CURL_BIN),
                "--silent",
                "--show-error",
                "--head",
                "--noproxy",
                "*",
                "--connect-timeout",
                str(SOCKET_TIMEOUT),
                "--max-time",
                str(SOCKET_TIMEOUT + 2),
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code} %{time_total}",
                url,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=SOCKET_TIMEOUT + 4,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise OSError("service-route HTTPS probe process timed out") from error
    fields = completed.stdout.strip().split()
    if (
        completed.returncode != 0
        or len(fields) != 2
        or not fields[0].startswith(("2", "3", "4"))
    ):
        raise OSError(
            f"service-route HTTPS payload check failed with curl exit {completed.returncode}"
        )
    return float(fields[1]) * 1000


class RoundTimeout(TimeoutError):
    pass


@contextmanager
def round_deadline(seconds: float | None = None) -> Iterator[None]:
    seconds = ROUND_TIMEOUT if seconds is None else seconds
    def expired(signum: int, frame: object) -> None:
        raise RoundTimeout(f"data round exceeded {seconds:g} seconds")

    previous = signal.signal(signal.SIGALRM, expired)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        signal.signal(signal.SIGALRM, previous)


def one_data_round(proxy: tuple[str, int]) -> tuple[bool, list[float], list[str]]:
    latencies: list[float] = []
    failures: list[str] = []
    telegram_https_ok = False
    for host, port in TELEGRAM_HTTPS_TARGETS:
        try:
            latencies.append(https_probe(proxy, host, port))
            telegram_https_ok = True
            break
        except OSError as error:
            failures.append(f"https:{host}:{type(error).__name__}")
    dc_successes = 0
    for target in TELEGRAM_DC_TARGETS:
        try:
            connection, latency = socks_connection(proxy, target)
            connection.close()
            latencies.append(latency)
            dc_successes += 1
        except OSError as error:
            failures.append(f"dc:{target[0]}:{type(error).__name__}")
    neutral_ok = False
    try:
        latencies.append(https_probe(proxy, *NEUTRAL_HTTPS_TARGET))
        neutral_ok = True
    except OSError as error:
        failures.append(f"https:{NEUTRAL_HTTPS_TARGET[0]}:{type(error).__name__}")
    return telegram_https_ok and dc_successes >= 1 and neutral_ok, latencies, failures


def probe_data(proxy: tuple[str, int], rounds: int) -> DataProbe:
    successes = 0
    latencies: list[float] = []
    failures: list[str] = []
    for _ in range(rounds):
        try:
            with round_deadline():
                passed, round_latencies, round_failures = one_data_round(proxy)
        except RoundTimeout:
            passed, round_latencies, round_failures = False, [], ["round_timeout"]
        successes += int(passed)
        latencies.extend(round_latencies)
        failures.extend(round_failures)
    required = rounds // 2 + 1
    return DataProbe(
        passed=successes >= required,
        rounds=rounds,
        successful_rounds=successes,
        latency_ms=statistics.median(latencies) if latencies else None,
        failures=failures[-12:],
    )


def one_service_route_round() -> tuple[bool, list[float], list[str]]:
    latencies: list[float] = []
    failures: list[str] = []
    telegram_ok = False
    for host, port in TELEGRAM_HTTPS_TARGETS:
        try:
            latencies.append(service_https_probe(host, port))
            telegram_ok = True
            break
        except OSError as error:
            failures.append(f"route:https:{host}:{type(error).__name__}")
    neutral_ok = False
    try:
        latencies.append(service_https_probe(*NEUTRAL_HTTPS_TARGET))
        neutral_ok = True
    except OSError as error:
        failures.append(
            f"route:https:{NEUTRAL_HTTPS_TARGET[0]}:{type(error).__name__}"
        )
    return telegram_ok and neutral_ok, latencies, failures


def probe_service_route(rounds: int) -> DataProbe:
    successes = 0
    latencies: list[float] = []
    failures: list[str] = []
    for _ in range(rounds):
        try:
            with round_deadline():
                passed, round_latencies, round_failures = one_service_route_round()
        except RoundTimeout:
            passed, round_latencies, round_failures = False, [], ["route:round_timeout"]
        successes += int(passed)
        latencies.extend(round_latencies)
        failures.extend(round_failures)
    required = rounds // 2 + 1
    return DataProbe(
        passed=successes >= required,
        rounds=rounds,
        successful_rounds=successes,
        latency_ms=statistics.median(latencies) if latencies else None,
        failures=failures[-12:],
    )


def wait_for_port(port: int, process: subprocess.Popen[bytes], timeout: float = 6) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"temporary Xray exited with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError("temporary Xray SOCKS port did not open")


@contextmanager
def temporary_proxy(profile: Path, port: int) -> Iterator[tuple[str, int]]:
    runtime_root = Path("/run/tg-vpn-failover")
    runtime_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime_root) as directory:
        config_path = Path(directory) / "config.json"
        atomic_json(config_path, normalized_config(profile, port))
        if not xray_config_is_valid(config_path):
            raise ValueError(f"Xray rejected probe profile {profile.name}")
        nobody = pwd.getpwnam("nobody")
        os.chown(directory, nobody.pw_uid, nobody.pw_gid)
        os.chown(config_path, nobody.pw_uid, nobody.pw_gid)

        process = subprocess.Popen(
            [
                str(RUNUSER_BIN),
                "-u",
                "nobody",
                "--",
                str(XRAY_BIN),
                "run",
                "-config",
                str(config_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            wait_for_port(port, process)
            yield "127.0.0.1", port
        finally:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)


def systemctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SYSTEMCTL_BIN), *arguments],
        check=check,
        capture_output=True,
        text=True,
        timeout=30,
    )


def xray_config_is_valid(path: Path) -> bool:
    completed = subprocess.run(
        [str(XRAY_BIN), "run", "-test", "-config", str(path)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return completed.returncode == 0


def transaction_path(active: Path, role: str) -> Path:
    # Xray chooses its config loader from the filename extension.
    return active.with_name(f".{active.stem}.{role}.json")


def secure_runtime_config(path: Path) -> None:
    service_user = pwd.getpwnam(XRAY_USER)
    os.chown(path, 0, service_user.pw_gid)
    os.chmod(path, 0o640)


def wait_for_production() -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        active = systemctl("is-active", "xray", check=False).returncode == 0
        try:
            with socket.create_connection(PRODUCTION_SOCKS, timeout=0.5):
                if active:
                    return
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("production Xray did not become ready")


def wait_for_service(service: str, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if systemctl("is-active", service, check=False).returncode == 0:
            return
        time.sleep(0.25)
    raise RuntimeError(f"{service} did not become ready")


def restart_transport() -> None:
    systemctl("reset-failed", "xray", "sing-box", check=False)
    systemctl("restart", "xray")
    wait_for_production()
    systemctl("restart", "sing-box")
    wait_for_service("sing-box")


def ensure_transport() -> None:
    services = ("xray", "sing-box")
    if all(systemctl("is-active", item, check=False).returncode == 0 for item in services):
        return
    secure_runtime_config(ACTIVE_CONFIG)
    restart_transport()
    log("transport_recovered")


def install_profile(profile: Path) -> Path:
    candidate = transaction_path(ACTIVE_CONFIG, "candidate")
    atomic_json(candidate, normalized_config(profile, PRODUCTION_SOCKS[1]))
    secure_runtime_config(candidate)
    if not xray_config_is_valid(candidate):
        candidate.unlink(missing_ok=True)
        raise ValueError(f"Xray rejected normalized profile {profile.name}")
    os.replace(candidate, ACTIVE_CONFIG)
    restart_transport()
    return ACTIVE_CONFIG


def backup_active() -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = BACKUP_DIR / f"config-{stamp}.json"
    shutil.copy2(ACTIVE_CONFIG, backup)
    os.chmod(backup, 0o600)
    return backup


def restore_backup(backup: Path) -> None:
    candidate = transaction_path(ACTIVE_CONFIG, "rollback")
    shutil.copy2(backup, candidate)
    secure_runtime_config(candidate)
    os.replace(candidate, ACTIVE_CONFIG)
    restart_transport()


def find_profiles() -> list[Path]:
    return sorted(path for path in PROFILE_DIR.glob("*.json") if path.is_file())


def profile_matches_active(profile: Path) -> bool:
    try:
        return normalized_config(profile, PRODUCTION_SOCKS[1]) == load_json(ACTIVE_CONFIG)
    except (OSError, ValueError, json.JSONDecodeError):
        return False


def should_run_deep(state: dict[str, Any], now: float) -> bool:
    last = float(state.get("last_deep_check_epoch") or 0)
    return now - last >= DEEP_CHECK_SECONDS


def profile_order(
    measurements: list[tuple[Path, LinkMeasurement]],
    cooldowns: dict[str, float],
    now: float,
) -> list[tuple[Path, LinkMeasurement]]:
    return sorted(
        measurements,
        key=lambda item: (
            float(cooldowns.get(item[0].stem, 0)) > now,
            item[1].order_score,
            item[0].name,
        ),
    )


def try_failover(state: dict[str, Any], *, dry_run: bool) -> bool:
    profiles = find_profiles()
    if not profiles:
        log("failover_unavailable", reason="no_profiles")
        state["status"] = "no_profiles"
        save_state(state)
        return False

    alternatives = [path for path in profiles if not profile_matches_active(path)]
    if not alternatives:
        log("failover_unavailable", reason="no_alternate_profiles", count=len(profiles))
        state["status"] = "no_alternate_profiles"
        save_state(state)
        return False

    measurements: list[tuple[Path, LinkMeasurement]] = []

    def measured(profile: Path) -> tuple[Path, LinkMeasurement] | None:
        try:
            return profile, measure_link(profile)
        except (OSError, ValueError, json.JSONDecodeError) as error:
            log("candidate_invalid", profile=profile.stem, error=str(error))
            return None

    # Keep this sequential. Starting a short-lived Xray process after a Python
    # thread pool proved unreliable on the target Debian/glibc combination.
    # Four or five regional link checks add only a few seconds and failover
    # correctness matters more than shaving that small preflight interval.
    for profile in alternatives:
        result = measured(profile)
        if result is not None:
            measurements.append(result)

    now = time.time()
    cooldowns = {
        str(key): float(value)
        for key, value in (state.get("cooldowns") or {}).items()
    }
    ordered = profile_order(measurements, cooldowns, now)
    backup: Path | None = None
    for index, (profile, measurement) in enumerate(ordered):
        port = PROBE_PORT_START + index
        try:
            with temporary_proxy(profile, port) as proxy:
                probe = probe_data(proxy, rounds=1)
        except Exception as error:
            probe = DataProbe(False, 1, 0, None, [type(error).__name__])
        log(
            "candidate_data",
            profile=profile.stem,
            link=asdict(measurement),
            probe=asdict(probe),
        )
        if not probe.passed:
            cooldowns[profile.stem] = now + COOLDOWN_SECONDS
            continue
        if dry_run:
            state.update(
                status="dry_run_candidate_found",
                selected_candidate=profile.stem,
                cooldowns=cooldowns,
            )
            save_state(state)
            return True

        backup = backup or backup_active()
        try:
            install_profile(profile)
            production_probe = probe_data(PRODUCTION_SOCKS, rounds=2)
            route_probe = probe_service_route(rounds=2)
            services_ok = all(
                systemctl("is-active", service, check=False).returncode == 0
                for service in ("xray", "sing-box", "tg-photode")
            )
            if not production_probe.passed or not route_probe.passed or not services_ok:
                raise RuntimeError("production verification failed")
        except Exception as error:
            log("candidate_rollback", profile=profile.stem, error=str(error))
            cooldowns[profile.stem] = now + COOLDOWN_SECONDS
            if backup is not None:
                restore_backup(backup)
            continue

        state.update(
            status="healthy",
            active_profile=profile.stem,
            selected_candidate=profile.stem,
            active_config_sha256=config_hash(ACTIVE_CONFIG),
            last_switch_at=utc_now(),
            consecutive_failures=0,
            cooldowns=cooldowns,
            last_probe=asdict(production_probe),
            last_route_probe=asdict(route_probe),
        )
        save_state(state)
        log(
            "failover_complete",
            profile=profile.stem,
            probe=asdict(production_probe),
            route_probe=asdict(route_probe),
        )
        return True

    state.update(status="no_working_candidate", cooldowns=cooldowns)
    save_state(state)
    log("failover_exhausted", candidates=len(ordered))
    return False


def run(*, force_deep: bool, force_failover: bool, dry_run: bool) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()
    now = time.time()

    if force_failover:
        return 0 if try_failover(state, dry_run=dry_run) else 2

    service_active = systemctl("is-active", "xray", check=False).returncode == 0
    route_services_active = service_active and (
        systemctl("is-active", "sing-box", check=False).returncode == 0
    )
    rounds = 3 if force_deep or should_run_deep(state, now) else 1
    try:
        current = probe_data(PRODUCTION_SOCKS, rounds=rounds) if service_active else DataProbe(
            False, rounds, 0, None, ["xray_inactive"]
        )
    except Exception as error:
        current = DataProbe(False, rounds, 0, None, [type(error).__name__])
    try:
        route_current = (
            probe_service_route(rounds=rounds)
            if route_services_active
            else DataProbe(False, rounds, 0, None, ["route_transport_inactive"])
        )
    except Exception as error:
        route_current = DataProbe(
            False,
            rounds,
            0,
            None,
            [f"route:{type(error).__name__}"],
        )

    if rounds > 1:
        state["last_deep_check_epoch"] = now
        state["last_deep_check_at"] = utc_now()
    state["last_probe"] = asdict(current)
    state["last_route_probe"] = asdict(route_current)
    log(
        "active_probe",
        deep=rounds > 1,
        probe=asdict(current),
        route_probe=asdict(route_current),
    )

    if current.passed and route_current.passed:
        state.update(status="healthy", consecutive_failures=0)
        save_state(state)
        return 0

    if current.passed and not route_current.passed:
        failures = int(state.get("consecutive_failures") or 0) + 1
        state.update(status="route_degraded", consecutive_failures=failures)
        last_recovery = float(state.get("last_route_recovery_epoch") or 0)
        should_recover = (
            not dry_run
            and route_services_active
            and failures >= QUICK_FAILURE_LIMIT
            and now - last_recovery >= ROUTE_RECOVERY_COOLDOWN_SECONDS
        )
        if should_recover:
            state["last_route_recovery_epoch"] = now
            state["last_route_recovery_at"] = utc_now()
            try:
                restart_transport()
                recovered = probe_service_route(rounds=1)
            except Exception as error:
                recovered = DataProbe(
                    False,
                    1,
                    0,
                    None,
                    [f"route_recovery:{type(error).__name__}"],
                )
            state["last_route_probe"] = asdict(recovered)
            log("route_recovery", probe=asdict(recovered))
            if recovered.passed:
                state.update(status="healthy", consecutive_failures=0)
                save_state(state)
                return 0
        save_state(state)
        log(
            "active_route_degraded",
            consecutive_failures=failures,
            action="restart_transport" if should_recover else "confirm_next_run",
        )
        return 1

    failures = int(state.get("consecutive_failures") or 0) + 1
    state.update(status="degraded", consecutive_failures=failures)
    save_state(state)
    immediate = not service_active or rounds > 1
    if not immediate and failures < QUICK_FAILURE_LIMIT:
        log("active_degraded", consecutive_failures=failures, action="confirm_next_run")
        return 1
    return 0 if try_failover(state, dry_run=dry_run) else 2


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force-deep", action="store_true")
    parser.add_argument("--force-failover", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--ensure-transport", action="store_true")
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    if arguments.status:
        print(json.dumps(load_state(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    if arguments.ensure_transport:
        try:
            ensure_transport()
            return 0
        except Exception as error:
            log("transport_recovery_failed", error=type(error).__name__, detail=str(error))
            return 3
    try:
        import fcntl

        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOCK_FILE.open("w", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("check_skipped", reason="already_running")
                return 0
            return run(
                force_deep=arguments.force_deep,
                force_failover=arguments.force_failover,
                dry_run=arguments.dry_run,
            )
    except Exception as error:
        log("controller_error", error=type(error).__name__, detail=str(error))
        return 3


if __name__ == "__main__":
    sys.exit(main())
