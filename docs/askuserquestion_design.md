# Design: `AskUserQuestion` support (interactive multiple-choice questions)

> **Historical note:** OpenCode has since been removed from the shipped server.
> References to its former lack of question support are retained only as context
> for the original design.

Status: **proposed / not yet implemented.** This document is a hand-off spec for
an implementing agent. It is self-contained: it includes the protocol findings
(verified empirically against Claude Code 2.1.183) so you do not have to
re-investigate.

## Problem

Claude Code has a built-in tool, **`AskUserQuestion`**, that the model uses to ask
the *user* a multiple-choice question (e.g. "Which emoji? Cat / Rocket / Taco")
and waits for the user's selection. The selection is the tool's **answer**, not a
permission allow/deny.

Today the agent-ui-server backend gates every tool through the permission path. So an
`AskUserQuestion` call arrives as a normal `approval_request` whose synthesized
`options` are just Allow / Deny. The front-end renders deny/accept; the real
choices (Cat/Rocket/Taco) are buried, unrendered, in `input.questions[].options`,
and there is no way for the user to actually answer.

**Goal:** surface `AskUserQuestion` as a real multiple-choice prompt and feed the
user's selection back to Claude as the tool's answer.

This is **Claude-only**. OpenCode (ACP) has no equivalent tool; see
[OpenCode](#opencode) below.

## Do NOT confuse with the existing "multiple-choice approvals"

The repo already shipped two *approval* features (see
`docs/tool_use_event_schema.md`): `approval_request.options` (allow_once /
allow_always / reject_…) and deny-with-`message`. Those are about **whether to run
a tool**. `AskUserQuestion` is a different axis entirely — it is the agent asking
the user to pick *content*. Keep it on its own event type; do not overload
`approval_request`.

## Protocol findings (verified)

How `AskUserQuestion` flows through
`claude -p --output-format stream-json --input-format stream-json
--permission-prompt-tool stdio --permission-mode default --verbose` (the exact
invocation in `ClaudeCodeAdapter.start_turn`):

1. The model emits an **assistant `tool_use` block** with `name:
   "AskUserQuestion"` and this input shape:

   ```json
   {
     "questions": [
       {
         "question": "Which emoji do you want?",
         "header": "Emoji",
         "multiSelect": false,
         "options": [
           { "label": "Cat",    "description": "The cat emoji 🐱" },
           { "label": "Rocket", "description": "The rocket emoji 🚀" },
           { "label": "Taco",   "description": "The taco emoji 🌮" }
         ]
       }
     ]
   }
   ```

2. Immediately after, a **`control_request`** arrives with
   `request.subtype == "can_use_tool"`, `request.tool_name == "AskUserQuestion"`,
   the same `input`, and a `request.tool_use_id`. This is the blocking call we
   must answer.

### How to ANSWER it (the key finding)

Respond to that `control_request` with a normal `control_response` carrying
`behavior: "allow"` and the answer merged into **`updatedInput`**. The
`AskUserQuestion` tool reads the answer back out of its own input. Two accepted
forms (both verified — Claude then proceeded correctly, e.g. replying `🚀`):

- **Structured (use this):** `updatedInput.answers`, an object keyed by the
  **exact question text** (NOT the `header`), value = the chosen option's
  `label`. For `multiSelect`, the value is an array of labels.

  ```jsonc
  // control_response.response.response:
  {
    "behavior": "allow",
    "updatedInput": {
      "questions": [ /* ...the original questions, unchanged... */ ],
      "answers": { "Which emoji do you want?": "Rocket" }
    }
  }
  ```
  → tool_result: *"Your questions have been answered: …"*

- **Free-text fallback:** `updatedInput.response` = a string → tool_result *"The
  user responded: …"*. Useful if the user types a custom answer instead of
  picking.

The CLI also reads optional `annotations` keyed by question text
(`{ "<question>": { "notes": "...", "preview": "..." } }`) — out of scope for v1,
but the field exists if we later add a notes box.

### Pitfalls (things that DON'T work — already tried)

- Keying `answers` by `header` ("Emoji") instead of the question text → recorded
  as **"the user did not answer the questions"** (`answers: {}`). The key MUST be
  the full question string.
- `allow` with `updatedInput` left unchanged → "did not answer".
- `updatedInput.questions[].answer = [...]` or filtering `options` to the choice
  → "did not answer".
- `deny` + `message: "The user selected: Rocket"` *does* work (the message
  becomes the tool_result and the model acts on it), but it is a hack — it logs
  the tool as denied. **Use the `allow` + `updatedInput.answers` path instead.**

Source of truth: the tool's `call()` echoes `answers`/`response`/`annotations`
from its input, and `mapToolResultToToolResultBlockParam` builds the result from
`answers[question]` or a top-level `response` (found in the Claude Code 2.1.183
binary).

## Proposed implementation

### New WebSocket events (add to `docs/tool_use_event_schema.md`)

**Server → client: `question`** (persisted/replayed, like `approval_request`):

```json
{
  "type": "question",
  "request_id": "perm_3f9a1c8e7b2d4a6f",
  "questions": [
    {
      "question": "Which emoji do you want?",
      "header": "Emoji",
      "multiSelect": false,
      "options": [
        { "label": "Cat",    "description": "The cat emoji 🐱" },
        { "label": "Rocket", "description": "The rocket emoji 🚀" },
        { "label": "Taco",   "description": "The taco emoji 🌮" }
      ]
    }
  ]
}
```

**Client → server: `question_response`**:

```json
{
  "type": "question_response",
  "request_id": "perm_3f9a1c8e7b2d4a6f",
  "answers": { "Which emoji do you want?": "Rocket" }
}
```

- `answers` is keyed by **question text** (the same string from the `question`
  event); value is a `label` string, or an array of `label`s when that question's
  `multiSelect` is true.
- The server validates that every key is a known question and every value is a
  known option `label` for that question; on mismatch send an `error` event and
  do not answer (leave the request pending so the client can retry).

**Server → client: `question_response`** broadcast confirmation (persisted),
mirrors `approval_response`: `{ "type": "question_response", "request_id",
"answers" }`.

### Backend: `ClaudeCodeAdapter` (`agent.py`)

1. **`__init__`**: add `self.pending_questions: dict[str,
   asyncio.Future[dict[str, Any]]] = {}`. Add a constant
   `QUESTION_TOOL = "AskUserQuestion"`.

2. **Suppress the duplicate tool_use.** In `_assistant_events`, skip the
   `tool_use`/`server_tool_use` block whose `name == QUESTION_TOOL` (it is
   surfaced as a `question` event via the control_request, so we don't also want
   a `tool_use` bubble for it).

3. **Branch in `_events_from_json`** on the `control_request`. After building
   `request = self._approval_request_event(event)` and reading `request_id` /
   `tool_input`, if `request["tool"] == QUESTION_TOOL`, route to a question
   handler instead of the approval flow:
   - register `self.pending_questions[request_id]` + `self.pending_sessions[...]`
   - `yield {"type": "question", "request_id", "questions":
     self._normalize_questions(tool_input)}`
   - `answers = await future`
   - write the `control_response` with `behavior: "allow"` and `updatedInput =
     {**tool_input, "answers": answers}`
   - `finally`: pop `pending_questions` + `pending_sessions`.

   Reuse the existing structure of the approval branch; the only differences are
   the event type, the future payload (`dict` of answers, not
   `ApprovalDecision`), and the `updatedInput.answers` write.

4. **`_normalize_questions(tool_input)`** → list of
   `{question, header, multiSelect: bool, options: [{label, description}]}`,
   guarding every field with defaults (the `input` is an opaque passthrough — do
   not assume keys, same convention as `tool_use`).

5. **`send_answer(self, session, request_id, answers)`** (override the base):
   - look up `pending_questions[request_id]`; `KeyError` if missing or
     `pending_sessions[request_id] != session["id"]` (mirror `send_approval`)
   - validate `answers` against the stored questions (see validation note above);
     `ValueError` on mismatch
   - `future.set_result(answers)` if not done.

   To validate, also stash the normalized questions per request (e.g.
   `self.pending_question_specs[request_id]`) when emitting the event, and clear
   it in the `finally` / `_clear_session_approvals`.

6. **`_clear_session_approvals`**: also fail + pop any `pending_questions` (and
   `pending_question_specs`) for the session, so a stop / process exit unblocks a
   pending question the same way it does a pending approval.

7. **Base class `AgentAdapter`**: add a concrete (non-abstract) `send_answer`
   that raises `NotImplementedError("This agent does not support interactive
   questions")`, so `OpenCodeAdapter` inherits a clean failure.

### Backend: `main.py`

1. **WebSocket loop**: handle `message_type == "question_response"` →
   `handle_question_answer(session_id, request_id, answers)`, catching
   `(KeyError, ValueError)` → `error` event (same pattern as
   `approval_response`).

2. **`handle_question_answer`**: `adapter.send_answer(...)`, then persist +
   broadcast a `question_response` event and flip status back to `running`
   (mirror `handle_approval`).

3. **`run_turn`**: treat `question` like `approval_request` — persist it, and set
   status to `awaiting_approval` (reuse the existing status; see note). Add
   `"question"` to the persisted-event set.

### Status value

Reuse the existing **`awaiting_approval`** status for a pending question (it means
"blocked on the user"); the client distinguishes a question from an approval by
the event type (`question` vs `approval_request`), not by status. This avoids a
new enum value. If a distinct indicator is wanted later, add `awaiting_question`
— but that is a breaking protocol addition the front-end must handle, so it is
out of scope for v1.

### multiSelect

When a question has `multiSelect: true`, the `question_response` value for that
question is an **array of labels**, and the backend writes `answers[question]` as
that array (the CLI renders an array fine). Single-select sends a bare string.

## OpenCode

OpenCode/ACP has no `AskUserQuestion` equivalent, so `question` events only ever
originate from the Claude adapter. `OpenCodeAdapter` does not implement
`send_answer` (inherits the base `NotImplementedError`). If a `question_response`
is somehow routed to an OpenCode session, it surfaces as an `error` event — which
is correct, since it cannot happen in normal operation. No OpenCode changes.

## Tests to add (`tests/test_core.py`)

- `_normalize_questions` extracts question/header/multiSelect/options with
  defaults for missing keys.
- `_assistant_events` **omits** a `tool_use` block whose `name ==
  "AskUserQuestion"` (and still emits other tool_use / text blocks).
- `send_answer` resolves the pending future; unknown `request_id` → `KeyError`;
  answer referencing an unknown question or option `label` → `ValueError`.
- A `control_response` shape check for the answer write: `behavior == "allow"`
  and `updatedInput.answers == {<question text>: <label>}` (drive
  `_write_question_response` with a fake/stub process that captures stdin).

## Live verification (do this before declaring done)

Mirror the probe that validated the design (uses real OpenAI/Anthropic creds):
drive `ClaudeCodeAdapter.start_turn` with a prompt that forces an
`AskUserQuestion` (e.g. *"Use AskUserQuestion to ask which emoji among Cat,
Rocket, Taco; after I answer reply with only the chosen emoji"*), assert a
`question` event is emitted, call `send_answer` with `{<question>: "Rocket"}`, and
confirm the turn completes with the model output reflecting the selection (`🚀`).
A denial/`updatedInput`-by-header path must NOT be used.

## Out of scope (v1)

- `annotations` (notes/preview per answer).
- A dedicated `awaiting_question` status.
- General mid-turn message queueing (separately pinned).
- Any OpenCode question mechanism.
