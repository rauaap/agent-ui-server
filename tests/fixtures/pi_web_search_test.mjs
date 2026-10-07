// Run the actual extension modules with mock credentials and HTTP responses.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { pathToFileURL } from "node:url";
const require = createRequire(process.argv[2]);
const { createJiti } = require("jiti");
const jiti = createJiti(import.meta.url, { alias: {
    "@earendil-works/pi-coding-agent": join(dirname(process.argv[2]), "..", "index.js"),
    typebox: require.resolve("typebox"),
} });
// Verify the installed SDK resolves environment-only credentials, not just mocks.
const credentialEnv = ["PI_OFFLINE", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"];
const savedCredentialEnv = Object.fromEntries(credentialEnv.map(name => [name, process.env[name]]));
const savedCredentialFetch = globalThis.fetch;
try {
    process.env.PI_OFFLINE = "1";
    process.env.ANTHROPIC_API_KEY = "test-env-only-anthropic";
    process.env.OPENAI_API_KEY = "test-env-only-openai";
    globalThis.fetch = async () => { throw new Error("Environment-only credential check must not fetch"); };
    const dist = join(dirname(process.argv[2]), "..");
    const { AuthStorage } = await import(pathToFileURL(join(dist, "core/auth-storage.js")));
    const { ModelRuntime } = await import(pathToFileURL(join(dist, "core/model-runtime.js")));
    const { ModelRegistry } = await import(pathToFileURL(join(dist, "core/model-registry.js")));
    const registry = new ModelRegistry(await ModelRuntime.create({
        credentials: AuthStorage.inMemory({}), modelsPath: null, allowModelNetwork: false,
    }));
    for (const [provider, key] of [
        ["anthropic", "test-env-only-anthropic"], ["openai", "test-env-only-openai"],
    ]) {
        const selected = registry.getAll().find(model => model.provider === provider);
        assert.ok(selected);
        assert.equal(registry.find(provider, selected.id), selected);
        const auth = await registry.getApiKeyAndHeaders(selected);
        assert.equal(auth.ok, true);
        assert.equal(auth.apiKey, key);
    }
} finally {
    globalThis.fetch = savedCredentialFetch;
    for (const name of credentialEnv) {
        if (savedCredentialEnv[name] === undefined) delete process.env[name];
        else process.env[name] = savedCredentialEnv[name];
    }
}
const root = process.argv[3];
const { callApiStream } = await jiti.import(join(root, "api.ts"));
const { getWebSearchModel, missingConfigResult, missingWebSearchConfigResult } = await jiti.import(join(root, "utils.ts"));
const { webSearch } = await jiti.import(join(root, "web_search.ts"));
const { urlContext } = await jiti.import(join(root, "url_context.ts"));
const prompt = { contents: [{ role: "user", parts: [{ text: "Search for Pi" }] }] };
const model = (provider, api) => ({ provider, api, id: "test-model", baseUrl: "https://model.example/v1", maxTokens: 8192 });
const ctx = (selected, auth) => ({ model: selected, modelRegistry: {
    getApiKeyAndHeaders: async () => auth,
    getAvailable: () => [selected],
    find: (provider, id) => provider === selected.provider && id === selected.id ? selected : undefined,
} });
const sse = (...events) => new Response(events.map(event => `data: ${typeof event === "string" ? event : JSON.stringify(event)}\n\n`).join(""), {
    headers: { "Content-Type": "text/event-stream" },
});
const previousFetch = globalThis.fetch;
const previousConfig = process.env.PI_WEB_SEARCH_CONFIG;
const previousOpenAIKey = process.env.OPENAI_API_KEY;
const dir = mkdtempSync(join(tmpdir(), "pi-web-search-"));
process.env.PI_WEB_SEARCH_CONFIG = join(dir, "web-search.json");
try {
    for (const [provider, api, suffix, event] of [
        ["github-copilot", "openai-responses", "/responses", { type: "response.output_text.delta", delta: "answer" }],
        ["openai", "openai-responses", "/responses", { type: "response.output_text.delta", delta: "answer" }],
        ["xai", "openai-responses", "/responses", { type: "response.output_text.delta", delta: "answer" }],
        ["anthropic", "anthropic-messages", "/messages", { type: "content_block_delta", delta: { type: "text_delta", text: "answer" } }],
        ["google", "google-generative-ai", "/models/test-model:streamGenerateContent?alt=sse", { candidates: [{ content: { parts: [{ text: "answer" }] } }] }],
    ]) {
        const selected = model(provider, api);
        const auth = { ok: true, apiKey: "resolved-key", baseUrl: "https://auth.example/v1" };
        globalThis.fetch = async (url, options) => {
            assert.equal(url, `https://auth.example/v1${suffix}`);
            assert.match(JSON.stringify(options.headers), /resolved-key/);
            return sse(event, "[DONE]");
        };
        assert.equal((await callApiStream(ctx(selected, auth), selected, prompt)).text, "answer");
    }

    const selected = model("github-copilot", "openai-responses");
    const unsupportedUrlContext = await urlContext("test", { query: "Pi", urls: ["https://pi.dev/"] }, undefined, undefined, ctx(selected, { ok: true, apiKey: "key" }));
    assert.equal(unsupportedUrlContext.isError, true);
    assert.equal(unsupportedUrlContext.details.error, "unsupported_provider");
    globalThis.fetch = async url => {
        assert.equal(url, "https://model.example/v1/responses");
        return sse({ type: "response.output_text.delta", delta: "answer" });
    };
    // Endpoint-less credentials use model configuration, never token parsing.
    await callApiStream(ctx(selected, { ok: true, apiKey: "token;proxy-ep=proxy.business.githubcopilot.com" }), selected, prompt);

    process.env.OPENAI_API_KEY = "must-not-override-resolved-auth";
    globalThis.fetch = async (_url, options) => {
        assert.equal(options.headers.authorization, "Bearer resolved-header");
        assert.doesNotMatch(JSON.stringify(options.headers), /must-not-override/);
        return sse({ type: "response.output_text.delta", delta: "answer" });
    };
    const openai = model("openai", "openai-responses");
    await callApiStream(ctx(openai, { ok: true, headers: { Authorization: "Bearer resolved-header" } }), openai, prompt);

    const codex = model("openai-codex", "openai-codex-responses");
    const payload = Buffer.from(JSON.stringify({ "https://api.openai.com/auth": { chatgpt_account_id: "account" } })).toString("base64url");
    globalThis.fetch = async (url, options) => {
        assert.equal(url, "https://model.example/v1/codex/responses");
        assert.equal(options.headers["chatgpt-account-id"], "account");
        return sse({ type: "response.output_text.delta", delta: "answer" }, { type: "response.completed", response: { output: [] } });
    };
    assert.equal((await callApiStream(ctx(codex, { ok: true, apiKey: `header.${payload}.signature` }), codex, prompt)).text, "answer");

    globalThis.fetch = async () => { throw new Error("should not fetch after auth failure"); };
    await assert.rejects(callApiStream(ctx(selected, { ok: false, error: "auth failed" }), selected, prompt), /auth failed/);

    let cancelled = false;
    globalThis.fetch = async () => new Response(new ReadableStream({
        start(controller) { controller.enqueue(new TextEncoder().encode('data: {broken\n\n')); },
        cancel() { cancelled = true; },
    }));
    await assert.rejects(callApiStream(ctx(selected, { ok: true, apiKey: "key" }), selected, prompt), SyntaxError);
    assert.equal(cancelled, true);
    writeFileSync(process.env.PI_WEB_SEARCH_CONFIG, JSON.stringify({ provider: selected.provider, model: selected.id }));
    const failed = await webSearch("test", { query: "Pi" }, undefined, undefined, ctx(selected, { ok: true, apiKey: "key" }));
    assert.equal(failed.isError, true);
    assert.match(failed.content[0].text, /JSON/);
    rmSync(process.env.PI_WEB_SEARCH_CONFIG);

    const google = model("google", "google-generative-ai");
    const redirect = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/test";
    const grounded = { candidates: [{ content: { parts: [{ text: "answer" }] }, groundingMetadata: { groundingChunks: [{ web: { uri: redirect, title: "Pi" } }] } }] };
    for (const failure of [new Error("redirect network failure"), new Response("failed", { status: 503 })]) {
        globalThis.fetch = async (_url, options) => {
            if (options.method === "POST") return sse(grounded);
            if (failure instanceof Error) throw failure;
            return failure;
        };
        await assert.rejects(callApiStream(ctx(google, { ok: true, apiKey: "key" }), google, prompt), /redirect network failure|API error \(503\)/);
    }
    globalThis.fetch = async (_url, options) => options.method === "POST" ? sse(grounded) : new Response(null, { status: 302, headers: { location: "https://pi.dev/" } });
    assert.equal((await callApiStream(ctx(google, { ok: true, apiKey: "key" }), google, prompt)).sources[0].url, "https://pi.dev/");

    const context = ctx(selected, { ok: true, apiKey: "key" });
    assert.equal(await getWebSearchModel(context), undefined); // explicit config missing must not switch models
    assert.equal(missingWebSearchConfigResult(context).details.error, "invalid_config");
    writeFileSync(process.env.PI_WEB_SEARCH_CONFIG, JSON.stringify({ provider: selected.provider, model: selected.id }));
    assert.equal(await getWebSearchModel(context), selected);
    writeFileSync(process.env.PI_WEB_SEARCH_CONFIG, JSON.stringify({ provider: selected.provider, modelId: selected.id }));
    assert.equal(await getWebSearchModel(context), undefined);
    assert.equal(missingWebSearchConfigResult(context).isError, true);
    context.modelRegistry.getAvailable = () => { throw new Error("registry broken"); };
    assert.throws(() => missingConfigResult(context), /registry broken/);
    console.log("pi-web-search cleanup checks passed");
} finally {
    globalThis.fetch = previousFetch;
    if (previousConfig === undefined) delete process.env.PI_WEB_SEARCH_CONFIG;
    else process.env.PI_WEB_SEARCH_CONFIG = previousConfig;
    if (previousOpenAIKey === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previousOpenAIKey;
    rmSync(dir, { recursive: true, force: true });
}
