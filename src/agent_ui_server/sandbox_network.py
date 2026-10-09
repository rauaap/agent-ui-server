"""Validation of server and project sandbox TCP destination exceptions."""
from __future__ import annotations

import ipaddress
from typing import Any


def validate_network_allowlist(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen = set()
    for entry in entries:
        address = ipaddress.IPv4Address(entry["ip"])
        if (address.is_loopback or address.is_multicast or address.is_unspecified
                or address.is_reserved or str(address) == "169.254.0.53"):
            raise ValueError("Sandbox network exceptions require a unicast IPv4 destination")
        port = entry["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Sandbox network exception port must be between 1 and 65535")
        key = (str(address), port)
        if key not in seen:
            seen.add(key)
            result.append({"ip": key[0], "port": port})
    return result
