#!/usr/bin/env python3
"""Create root-only regional Xray profiles without printing provider credentials."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import socket
import sys
from pathlib import Path
from typing import Any


REGION = re.compile(r"^[a-z0-9][a-z0-9-]{0,15}$")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("source must contain one JSON object")
    return config


def proxy_outbound(config: dict[str, Any]) -> dict[str, Any]:
    ignored = {"freedom", "blackhole", "dns", "direct", "block"}
    for outbound in config.get("outbounds", []):
        if isinstance(outbound, dict) and outbound.get("protocol") not in ignored:
            return outbound
    raise ValueError("source has no proxy outbound")


def endpoint_slot(outbound: dict[str, Any]) -> dict[str, Any]:
    settings = outbound.get("settings") or {}
    candidates = settings.get("vnext") or settings.get("servers") or []
    if not candidates or not isinstance(candidates[0], dict):
        raise ValueError("proxy outbound has no endpoint")
    return candidates[0]


def regional_config(
    source: dict[str, Any], *, host: str, base_domain: str
) -> dict[str, Any]:
    config = copy.deepcopy(source)
    outbound = proxy_outbound(config)
    endpoint_slot(outbound)["address"] = host
    stream = outbound.get("streamSettings") or {}
    for security_key in ("realitySettings", "tlsSettings"):
        security = stream.get(security_key)
        if not isinstance(security, dict):
            continue
        server_name = security.get("serverName")
        if isinstance(server_name, str) and (
            server_name == base_domain or server_name.endswith(f".{base_domain}")
        ):
            security["serverName"] = host
    return config


def atomic_profile(path: Path, config: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def resolves(host: str) -> bool:
    try:
        return bool(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
    except socket.gaierror:
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--profile-dir", type=Path, required=True)
    parser.add_argument("--base-domain", required=True)
    parser.add_argument("regions", nargs="+")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    base_domain = arguments.base_domain.strip().lower().rstrip(".")
    if not base_domain or "." not in base_domain:
        raise ValueError("base domain is invalid")
    source = load_config(arguments.source)
    arguments.profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(arguments.profile_dir, 0o700)
    created: list[dict[str, str]] = []
    skipped: list[str] = []
    for raw_region in arguments.regions:
        region = raw_region.strip().lower()
        if not REGION.fullmatch(region):
            raise ValueError(f"invalid region label: {raw_region}")
        host = f"{region}.{base_domain}"
        if not resolves(host):
            skipped.append(region)
            continue
        path = arguments.profile_dir / f"{region}.json"
        atomic_profile(
            path,
            regional_config(source, host=host, base_domain=base_domain),
        )
        created.append({"region": region, "endpoint": host, "profile": str(path)})
    print(json.dumps({"created": created, "skipped": skipped}, separators=(",", ":")))
    return 0 if created else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        print(
            json.dumps(
                {"error": type(error).__name__, "detail": str(error)},
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        sys.exit(1)
