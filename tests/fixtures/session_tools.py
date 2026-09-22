#!/usr/bin/env python3
"""Simulated harness peer for both approved session-tool transports."""
import json
import sys


def read():
    return json.loads(sys.stdin.readline())


def write(value):
    print(json.dumps(value), flush=True)


names = ["message_session", "start_session", "read_session"]
pi = "--mode" in sys.argv
if pi:
    assert "--agent-ui-session-tools" in sys.argv
    write({"type": "extension_ui_request", "method": "notify", "message": json.dumps({
        "agent-ui": 1, "kind": "ready", "sessionTools": names,
    })})
    while True:
        request = read()
        if request["type"] == "prompt":
            config = json.loads(request["message"])
            break
        if request["type"] == "get_state":
            write({"type": "response", "command": "get_state", "id": request["id"],
                   "success": True, "data": {"sessionId": "test-session"}})
    write({"type": "tool_execution_start", "toolCallId": "call",
           "toolName": config["name"], "args": config["arguments"]})
    write({"type": "extension_ui_request", "method": "input", "id": "call", "title": json.dumps({
        "agent-ui": 1, "kind": "session_tool", "toolCallId": "call",
        "name": config["name"], "arguments": config["arguments"],
    })})
    response = read()
    if response.get("type") == "test_cancel":
        write({"type": "extension_ui_request", "method": "notify", "message": json.dumps({
            "agent-ui": 1, "kind": "host_cancel", "toolCallId": "call",
        })})
        response = read()
    write({"type": "message_update", "assistantMessageEvent": {
        "type": "text_delta", "delta": json.dumps(response),
    }})
    write({"type": "agent_settled"})
else:
    assert "--mcp-config" in sys.argv
    init = read()
    write({"type": "control_response", "response": {
        "subtype": "success", "request_id": init["request_id"], "response": {},
    }})
    config = json.loads(read()["message"]["content"])
    def mcp(mid, method, **params):
        write({"type": "control_request", "request_id": mid, "request": {
            "subtype": "mcp_message", "server_name": "agent_ui", "message": {
                "jsonrpc": "2.0", "id": mid, "method": method, "params": params,
            },
        }})
    mcp("list", "tools/list")
    tools = read()["response"]["response"]["mcp_response"]["result"]["tools"]
    assert [t["name"] for t in tools] == names
    write({"type": "control_request", "request_id": "permission", "request": {
        "subtype": "can_use_tool", "tool_name": "mcp__agent_ui__" + config["name"],
        "input": config["arguments"],
    }})
    assert read()["response"]["response"]["behavior"] == "allow"
    write({"type": "assistant", "message": {"content": [{
        "type": "tool_use", "id": "toolu_call", "name": "mcp__agent_ui__" + config["name"],
        "input": config["arguments"],
    }]}})
    mcp("call", "tools/call", **config, _meta={"claudecode/toolUseId": "toolu_call"})
    response = read()
    if response.get("type") == "test_cancel":
        write({"type": "control_cancel_request", "request_id": "call"})
    else:
        write({"type": "assistant", "message": {"content": [{
            "type": "text", "text": json.dumps(response["response"]["response"]["mcp_response"]),
        }]}})
    write({"type": "result", "session_id": "test-session"})
