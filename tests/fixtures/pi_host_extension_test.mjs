// Exercise the actual TypeScript tool with a mock ExtensionAPI (no model calls).
import assert from "node:assert/strict";
import { createRequire } from "node:module";
const require = createRequire(process.argv[2]);
const { createJiti } = require("jiti");
const jiti = createJiti(import.meta.url, { alias: { typebox: require.resolve("typebox") } });
const extension = await jiti.import(process.argv[3], { default: true });
const serverTools = process.argv[4] ? JSON.parse(process.argv[4]) : [];
// TypeBox metadata is symbol-keyed. Ignore presentation/default annotations,
// comparing the actual accepted JSON shapes with the Python tool definitions.
function schemaShape(value) {
    if (Array.isArray(value)) return value.map(schemaShape);
    if (value && typeof value === "object") return Object.fromEntries(
        Object.entries(value).filter(([key]) => !["title", "default"].includes(key))
            .map(([key, child]) => [key, schemaShape(child)]),
    );
    return value;
}

for (const enabled of [false, true]) {
  for (const sessionEnabled of [false, true]) {
    const tools = new Map(), handlers = new Map(), notices = [];
    const api = {
        registerFlag() {}, getFlag: (name) => name === "agent-ui-host-exec" ? enabled : sessionEnabled,
        registerTool: (tool) => tools.set(tool.name, tool),
        on: (name, callback) => handlers.set(name, callback),
    };
    extension(api);
    const ctx = { mode: "rpc", hasUI: true, ui: {
        notify: (message) => notices.push(JSON.parse(message)),
        input: async () => { throw new Error("unexpected input"); },
        select: async () => { throw new Error("duplicate approval prompt"); },
    } };
    await handlers.get("session_start")({}, ctx);
    assert.equal(tools.has("bypass_sandbox"), enabled);
    const sessionNames = ["message_session", "start_session", "read_session"];
    assert.deepEqual(notices[0].sessionTools, sessionEnabled ? sessionNames : undefined);
    for (const name of sessionNames) {
        assert.equal(tools.has(name), sessionEnabled);
        if (!sessionEnabled) continue;
        const sessionTool = tools.get(name);
        const serverTool = serverTools.find(tool => tool.name === name);
        if (serverTool) {
            assert.equal(sessionTool.description, serverTool.description);
            assert.deepEqual(schemaShape(sessionTool.parameters), schemaShape(serverTool.inputSchema));
        }
        assert.equal(sessionTool.parameters.additionalProperties, false);
        assert.equal(await handlers.get("tool_call")({ toolName: name }, ctx), undefined);
        for (const isError of [false, true]) {
            ctx.ui.input = async (title) => {
                assert.deepEqual(JSON.parse(title), {
                    "agent-ui": 1, kind: "session_tool", name, toolCallId: "s1", arguments: { session_id: 42 },
                });
                return JSON.stringify({ isError, content: [{ type: "text", text: "session result" }] });
            };
            const execution = sessionTool.execute("s1", { session_id: 42 }, undefined, undefined, ctx);
            if (isError) await assert.rejects(execution, /session result/);
            else assert.deepEqual((await execution).content, [{ type: "text", text: "session result" }]);
        }
        await assert.rejects(sessionTool.execute("s1", {}, undefined, undefined, { ...ctx, mode: "tui" }), /RPC/);
    }
    if (!enabled) continue;
    const tool = tools.get("bypass_sandbox");
    const args = { command: "printf hello", reason: "test" };
    assert.equal(await handlers.get("tool_call")({ toolName: tool.name }, ctx), undefined);
    assert.match(tool.promptSnippet, /outside the sandbox/);
    assert.equal(tool.parameters.additionalProperties, false);
    for (const isError of [false, true]) {
        ctx.ui.input = async (title) => {
            assert.deepEqual(JSON.parse(title), {
                "agent-ui": 1, kind: "host_exec", toolCallId: "c1", arguments: args,
            });
            return JSON.stringify({ isError, content: [{ type: "text", text: "result" }] });
        };
        const execution = tool.execute("c1", args, new AbortController().signal, undefined, ctx);
        if (isError) await assert.rejects(execution, /result/);
        else assert.deepEqual((await execution).content, [{ type: "text", text: "result" }]);
    }
    const controller = new AbortController();
    ctx.ui.input = (_, __, { signal }) => new Promise((resolve) => {
        signal.addEventListener("abort", () => resolve(undefined), { once: true });
    });
    const execution = tool.execute("cancel", args, controller.signal, undefined, ctx);
    controller.abort();
    await assert.rejects(execution, /cancelled/);
    assert.deepEqual(notices.at(-1), { "agent-ui": 1, kind: "host_cancel", toolCallId: "cancel" });
    await assert.rejects(tool.execute("c1", args, undefined, undefined, { ...ctx, mode: "tui" }), /RPC/);
  }
}
