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


def stream_call(config):
    raw = json.dumps(config["arguments"])
    name = config["name"] if pi else "mcp__agent_ui__" + config["name"]
    call_id = "call" if pi else "toolu_call"
    block = {"type": "toolCall" if pi else "tool_use", "id": call_id, "name": name,
             "arguments" if pi else "input": {}}
    def partial(event):
        write({"type": "message_update", "assistantMessageEvent": event} if pi else
              {"type": "stream_event", "event": event})
    if pi:
        write({"type": "message_start", "message": {"role": "assistant", "content": []}})
        partial({"type": "toolcall_start", "contentIndex": 0, "id": call_id, "toolName": name})
    else:
        partial({"type": "message_start", "message": {"id": "streamed-message"}})
        partial({"type": "content_block_start", "index": 0, "content_block": block})
    for index in range(0, len(raw), 128):
        delta = raw[index:index + 128]
        if pi:
            partial({"type": "toolcall_delta", "contentIndex": 0, "delta": delta})
        else:
            partial({"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": delta}})
    if pi:
        partial({"type": "toolcall_end", "contentIndex": 0, "toolCall": {**block, "arguments": config["arguments"]}})
    else:
        block["input"] = config["arguments"]
        write({"type": "assistant", "message": {"id": "streamed-message", "content": [block]}})
        partial({"type": "content_block_stop", "index": 0})
if pi:
    write({"type": "extension_ui_request", "method": "notify", "message": json.dumps({
        "agent-ui": 1, "kind": "ready", "sessionTools": names,
        "assetTool": "resolve_asset_link",
    })})
    while True:
        request = read()
        if request["type"] == "prompt":
            config = json.loads(request["message"])
            break
        if request["type"] == "get_state":
            write({"type": "response", "command": "get_state", "id": request["id"],
                   "success": True, "data": {"sessionId": "test-session"}})
    if config.get("stream"):
        stream_call(config)
    write({"type": "tool_execution_start", "toolCallId": "call",
           "toolName": config["name"], "args": config["arguments"]})
    write({"type": "extension_ui_request", "method": "input", "id": "call", "title": json.dumps({
        "agent-ui": 1, "kind": "asset_tool" if config["name"] == "resolve_asset_link" else "session_tool", "toolCallId": "call",
        "name": config["name"], "arguments": config["arguments"],
    })})
    response = read()
    if response.get("type") == "test_cancel":
        write({"type": "extension_ui_request", "method": "notify", "message": json.dumps({
            "agent-ui": 1, "kind": "host_cancel", "toolCallId": "call",
        })})
        response = read()
    write({"type": "message_start", "message": {"role": "assistant", "content": []}})
    write({"type": "message_update", "assistantMessageEvent": {
        "type": "text_end", "contentIndex": 0, "content": json.dumps(response),
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
    assert [t["name"] for t in tools] == names + ["resolve_asset_link"]
    write({"type": "control_request", "request_id": "permission", "request": {
        "subtype": "can_use_tool", "tool_name": "mcp__agent_ui__" + config["name"],
        "input": config["arguments"], "tool_use_id": "toolu_call",
    }})
    assert read()["response"]["response"]["behavior"] == "allow"
    if config.get("stream"):
        stream_call(config)
    else:
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
