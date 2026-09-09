# Vendored: pi-web-search

Provider-native web search for Pi, giving Pi sessions the `web_search` and
`url_context` tools that `PiAdapter` normalizes into canonical `web` actions.

| | |
|---|---|
| Upstream | https://github.com/ttttmr/pi-web-search |
| Version | 1.5.0 |
| License | MIT |
| Vendored | 2026-09-09 |

The `.ts` files in this directory are copied verbatim from the published npm
tarball (`pi-web-search-1.5.0.tgz`, `src/`), which is byte-identical to the
upstream repository at that version. Keep them that way: local modifications
turn every future update into a merge. Behavior that agent-ui-server needs to
control belongs in `pi_extension.ts` or `PiAdapter` instead.

## Why vendored rather than installed

`pi install npm:pi-web-search` records the extension in Pi's settings, and
`PiAdapter` runs Pi with `--no-extensions` so that a project's own extensions
cannot mutate tool input after the user has approved it. Discovered extensions
are exactly what that flag drops. Passing this directory's `index.ts` as a
second explicit `-e` keeps the extension while preserving that guarantee.

Vendoring also keeps the wheel self-contained: hatchling packages everything
under `src/agent_ui_server`, so a `pip install` of agent-ui-server carries the
extension the same way it carries `pi_extension.ts`.

## Updating

```sh
npm pack pi-web-search@<version>
tar xzf pi-web-search-<version>.tgz
cp package/src/*.ts src/agent_ui_server/pi_web_search/
```

Then re-read the diff before committing. Two things matter to us: the tool
names `web_search` and `url_context`, which `pi_extension.ts` allowlists and
`tool_actions.py` translates, and the argument spellings `query` and `urls`,
which those translators project into the canonical schema. A rename upstream is
silent here — the gate would start prompting for an unrecognized tool and the
action would fall back to `other` — so `test_actions.py` asserts both.

## Runtime requirements

The extension calls whichever provider backs the session's current model
(Gemini, xAI, OpenAI/Azure/Codex/Copilot, or Anthropic) using the credentials
Pi already holds, so a subscription login via `pi` is sufficient and no
additional API key is required. Each `web_search` call is a full inference
request against that provider, billed or rate-limited like any other turn.

`url_context` is Gemini-only; on other providers it returns an explanatory
error rather than failing, and `index.ts` hides the tool entirely when the
current model is not Gemini.
