from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "ops" / "tg_vpn_seed_regions.py"
SPEC = importlib.util.spec_from_file_location("tg_vpn_seed_regions", MODULE_PATH)
assert SPEC and SPEC.loader
regions = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = regions
SPEC.loader.exec_module(regions)


def test_regional_config_changes_only_endpoint_and_matching_sni() -> None:
    source = {
        "inbounds": [{"protocol": "socks", "port": 18082}],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {
                    "vnext": [
                        {
                            "address": "fl.convert-flow.net",
                            "port": 443,
                            "users": [{"id": "secret"}],
                        }
                    ]
                },
                "streamSettings": {
                    "security": "reality",
                    "realitySettings": {
                        "serverName": "fl.convert-flow.net",
                        "publicKey": "secret-public-key",
                    },
                },
            }
        ],
    }

    result = regions.regional_config(
        source,
        host="de.convert-flow.net",
        base_domain="convert-flow.net",
    )

    outbound = result["outbounds"][0]
    assert outbound["settings"]["vnext"][0]["address"] == "de.convert-flow.net"
    assert outbound["streamSettings"]["realitySettings"]["serverName"] == "de.convert-flow.net"
    assert outbound["settings"]["vnext"][0]["users"][0]["id"] == "secret"
    assert source["outbounds"][0]["settings"]["vnext"][0]["address"] == "fl.convert-flow.net"


def test_non_provider_sni_is_not_rewritten() -> None:
    source = {
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {"vnext": [{"address": "old.example", "port": 443}]},
                "streamSettings": {
                    "tlsSettings": {"serverName": "cover.example"}
                },
            }
        ]
    }

    result = regions.regional_config(
        source,
        host="nl.convert-flow.net",
        base_domain="convert-flow.net",
    )

    assert result["outbounds"][0]["streamSettings"]["tlsSettings"]["serverName"] == "cover.example"
