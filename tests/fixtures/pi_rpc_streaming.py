#!/usr/bin/env python3
"""Replay Pi-serialized native wire events without running any model or tool."""
import json
import sys
from pathlib import Path


def emit(record):
    print(json.dumps(record), flush=True)


emit({"type": "extension_ui_request", "method": "notify", "message": json.dumps({
    "agent-ui": 1, "kind": "ready", "sessionTools": ["message_session", "start_session", "read_session"],
    "assetTool": "resolve_asset_link",
})})
for line in sys.stdin:
    request = json.loads(line)
    if request["type"] == "get_state":
        emit({"type": "response", "command": "get_state", "success": True,
              "data": {"sessionId": "pi-rpc-fixture"}})
    elif request["type"] == "prompt":
        fixture = json.loads(Path(__file__).with_name("pi_1_1_0_rpc_streaming.json").read_text())
        for event in fixture["events"]:
            emit(event)
        break
