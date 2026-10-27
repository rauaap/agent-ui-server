// Generate deterministic RPC wire fixtures with Pi's installed serializer.
// Usage: node generate_pi_rpc_streaming.mjs /absolute/path/to/dist/modes/json-event.js
// No models, credentials, sessions, or tools are used.
import { pathToFileURL } from 'node:url';
import { readFileSync } from 'node:fs';
const moduleUrl = pathToFileURL(process.argv[2]);
const { toJsonEvent } = await import(moduleUrl);
const { version } = JSON.parse(readFileSync(new URL('../../package.json', moduleUrl), 'utf8'));
const events = [];
const usage = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const message = { role: 'assistant', content: [], usage, stopReason: 'pending', timestamp: 1 };
const emit = event => events.push(structuredClone(toJsonEvent(event)));
const update = event => emit({ type: 'message_update', message, assistantMessageEvent: { ...event, partial: message } });
emit({ type: 'message_start', message });
message.content.push({ type: 'thinking', thinking: 'not public' });
update({ type: 'thinking_start', contentIndex: 0 });
update({ type: 'thinking_delta', contentIndex: 0, delta: 'not public' });
update({ type: 'thinking_end', contentIndex: 0, content: 'not public' });
message.content.push({ type: 'text', text: '' });
update({ type: 'text_start', contentIndex: 1 });
const args = { message: 'Hello "quoted" \\ path\nemoji 😀', session_id: 42 };
const call = { type: 'toolCall', id: 'rpc-call', name: 'message_session', arguments: {} };
message.content.push(call);
update({ type: 'toolcall_start', contentIndex: 2 });
message.content[1].text = 'Hello';
update({ type: 'text_delta', contentIndex: 1, delta: 'Hello' });
const raw = JSON.stringify(args).replace(/[\u007f-\uffff]/g, char => `\\u${char.charCodeAt(0).toString(16).padStart(4, '0')}`);
for (const delta of raw) {
  // The SDK snapshot is intentionally present; toJsonEvent must remove it.
  update({ type: 'toolcall_delta', contentIndex: 2, delta });
}
message.content[1].text += ' world';
update({ type: 'text_delta', contentIndex: 1, delta: ' world' });
message.content[1].text = 'Hello world!';
update({ type: 'text_end', contentIndex: 1, content: message.content[1].text });
call.arguments = args;
update({ type: 'toolcall_end', contentIndex: 2, toolCall: call });
message.stopReason = 'toolUse';
emit({ type: 'message_end', message });
emit({ type: 'tool_execution_start', toolCallId: call.id, toolName: call.name, args });
emit({ type: 'agent_settled', aborted: false });
console.log(JSON.stringify({
  capture: { harness: 'Pi', version, generated_using_installed_serializer: true,
    source: 'dist/modes/json-event.js:toJsonEvent', synthetic_input: true,
    observations: ['RPC omits partial and message snapshots on updates', 'toolcall_start supplies id and toolName', 'raw JSON is split at every escape boundary'] },
  expected: { text: message.content[1].text, arguments_json: raw, arguments: args }, events,
}, null, 2));
