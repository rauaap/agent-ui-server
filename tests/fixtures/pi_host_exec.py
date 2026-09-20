#!/usr/bin/env python3
"""Simulated Pi peer for host-tool bridge tests; no model calls."""
import json
import sys


def emit(message):
    print(json.dumps(message), flush=True)


def envelope(kind, **payload):
    return json.dumps({"agent-ui": 1, "kind": kind, **payload})


emit({"type": "extension_ui_request", "id": "ready", "method": "notify",
      "message": envelope("ready", hostTool=(
          "bypass_sandbox" if "--agent-ui-host-exec" in sys.argv else None))})
pending = set()
for line in sys.stdin:
    message = json.loads(line)
    if message["type"] == "get_state":
        emit({"type": "response", "command": "get_state", "success": True,
              "data": {"sessionId": "pi-host-fixture"}})
    elif message["type"] == "prompt":
        spec = json.loads(message["message"])
        for index, args in enumerate(spec["calls"]):
            call_id = f"call-{index}"
            pending.add(call_id)
            emit({"type": "tool_execution_start", "toolCallId": call_id,
                  "toolName": "bypass_sandbox", "args": args})
            emit({"type": "extension_ui_request", "method": "input", "id": call_id,
                  "title": envelope("host_exec", toolCallId=call_id, arguments=args)})
    elif message["type"] == "test_cancel":
        emit({"type": "extension_ui_request", "method": "notify", "id": "cancel",
              "message": envelope("host_cancel", toolCallId=message["call_id"])})
    elif message["type"] == "extension_ui_response":
        emit({"type": "message_update", "assistantMessageEvent": {
            "type": "text_end", "content": json.dumps(message),
        }})
        pending.discard(message["id"])
        if not pending:
            emit({"type": "agent_settled"})
