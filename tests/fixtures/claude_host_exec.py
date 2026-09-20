#!/usr/bin/env python3
"""Fake Claude peer exercising SDK MCP over actual subprocess pipes."""
import json
import sys


def read():
    return json.loads(sys.stdin.readline())


def write(message):
    print(json.dumps(message), flush=True)


def mcp(mid, method, **params):
    write({"type": "control_request", "request_id": f"mcp-{mid}", "request": {
        "subtype": "mcp_message", "server_name": "agent_ui",
        "message": {"jsonrpc": "2.0", "id": mid, "method": method, "params": params},
    }})
    return read()["response"]["response"]["mcp_response"]


init = read()
assert init["request"]["subtype"] == "initialize"
write({"type": "control_response", "response": {
    "subtype": "success", "request_id": init["request_id"], "response": {},
}})
assert read()["type"] == "user"
assert mcp(1, "initialize")["result"]["capabilities"] == {"tools": {}}
tool = mcp(2, "tools/list")["result"]["tools"][0]
assert tool["name"] == "bypass_sandbox"
assert "_meta" not in tool
write({"type": "control_request", "request_id": "permission", "request": {
    "subtype": "can_use_tool", "tool_name": "mcp__agent_ui__bypass_sandbox",
    "input": {"command": "printf prototype", "reason": "test"},
}})
assert read()["response"]["response"]["behavior"] == "allow"
result = mcp(3, "tools/call", name="bypass_sandbox", arguments={
    "command": "printf prototype", "reason": "test",
})
write({"type": "assistant", "message": {"content": [
    {"type": "text", "text": json.dumps(result["result"])},
]}})
write({"type": "result", "session_id": "fake-host-session"})
