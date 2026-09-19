/**
 * agent-ui-server's Pi extension — approval gate + AskUserQuestion.
 *
 * Pi ships no permission system by design ("built-in tools can read files,
 * write files, edit files, and run shell commands with the permissions of the
 * Pi process") and no tool for asking the user a question. Both are things
 * agent-ui-server's wire contract requires, so both are built here. This file is
 * the Pi-side counterpart of PiAdapter and is passed to the child with `-e`.
 *
 * Transport. In RPC mode `ctx.ui.select` / `ctx.ui.input` are not dialogs — Pi
 * swaps in implementations that write an `extension_ui_request` line to stdout
 * and resolve when the client answers with `extension_ui_response` on stdin.
 * The client is PiAdapter, which relays to the phone. Neither call has a field
 * for structured data, so everything the adapter needs to build its wire event
 * is JSON-encoded into `title`; see `envelope`.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

/** Bumped when the `title` envelope shape changes; PiAdapter checks it. */
const PROTOCOL_VERSION = 1;

/**
 * Marker key identifying an envelope as ours. Pi merges extensions from three
 * sources (project-local, global, `-e`), so the adapter cannot assume every
 * `extension_ui_request` it sees came from this file.
 */
const MARKER = "agent-ui";

/**
 * Tools that never reach the gate. Claude Code's adapter auto-approves its
 * equivalent read-only allowlist; Pi hands us every call, so its list lives here.
 *
 * Deliberately an allowlist: an unrecognized tool (another extension's, or one
 * added by a future Pi release) prompts rather than slipping through.
 *
 * `web_search` and `url_context` come from the vendored pi-web-search
 * extension. They are listed here because they change nothing on disk, matching
 * the Claude adapter's auto-approval of WebSearch and WebFetch.
 * Note also that these are network egress and each
 * call spends an inference request on the session's provider — read-only
 * locally, not free.
 */
const READ_ONLY_TOOLS = new Set([
	"read",
	"grep",
	"find",
	"ls",
	"web_search",
	"url_context",
]);

const QUESTION_TOOL = "AskUserQuestion";

const ALLOW = "Allow";
const DENY = "Deny";

interface QuestionOption {
	label: string;
	description: string;
}

interface Question {
	question: string;
	header: string;
	multiSelect: boolean;
	options: QuestionOption[];
}

/**
 * Encode a payload for the `title` field of a dialog request.
 *
 * `title` is the only free-form string travelling outbound — `select` takes
 * `options: string[]` and `input` takes a placeholder — so correlation ids and
 * question metadata have to ride in it. The adapter parses `title` as JSON and
 * ignores anything without the marker key.
 */
function envelope(kind: string, payload: Record<string, unknown>): string {
	return JSON.stringify({ [MARKER]: PROTOCOL_VERSION, kind, ...payload });
}

/**
 * Coerce the model's tool arguments into the shape PiAdapter expects.
 *
 * Tool input is validated against the schema before we see it, but it is still
 * model output: guard every field with a default rather than trusting it, the
 * same convention the Python side uses in `_normalize_questions`.
 *
 * `multiSelect` is forced false. One `select` yields one answer, so "pick all
 * that apply" cannot be expressed over this transport; the schema does not
 * offer the flag, and pinning it here keeps the adapter's event honest.
 */
function normalizeQuestions(raw: unknown): Question[] {
	if (!Array.isArray(raw)) return [];

	const questions: Question[] = [];
	for (const entry of raw) {
		if (typeof entry !== "object" || entry === null) continue;
		const source = entry as Record<string, unknown>;

		const options: QuestionOption[] = [];
		if (Array.isArray(source.options)) {
			for (const option of source.options) {
				if (typeof option !== "object" || option === null) continue;
				const fields = option as Record<string, unknown>;
				const label = String(fields.label ?? "").trim();
				// A label is the answer's identity — the adapter validates the
				// client's pick against it — so an unlabelled option is dropped
				// rather than turned into an unselectable empty string.
				if (!label) continue;
				options.push({ label, description: String(fields.description ?? "") });
			}
		}
		if (options.length === 0) continue;

		const question = String(source.question ?? "").trim();
		if (!question) continue;

		questions.push({
			question,
			header: String(source.header ?? "").trim(),
			multiSelect: false,
			options,
		});
	}
	return questions;
}

/**
 * Parameter schema for the question tool.
 *
 * Must be a real TypeBox schema rather than a plain JSON Schema literal: Pi
 * types `ToolDefinition.parameters` as `TSchema` and validates the model's
 * arguments against it, which relies on TypeBox's symbol-keyed metadata. The
 * `typebox` specifier is aliased for extensions, so this resolves wherever the
 * file is loaded from.
 */
const QUESTION_SCHEMA = Type.Object({
	questions: Type.Array(
		Type.Object({
			question: Type.String({
				description: "The complete question, ending in a question mark.",
			}),
			header: Type.String({
				description: "Short label for the question (max 12 chars), e.g. 'Auth method'.",
			}),
			options: Type.Array(
				Type.Object({
					label: Type.String({ description: "The choice, 1-5 words." }),
					description: Type.String({ description: "What this choice means or implies." }),
				}),
				{ minItems: 2, maxItems: 4 },
			),
		}),
		{
			minItems: 1,
			maxItems: 4,
			description: "The questions to ask, all shown to the user at once.",
		},
	),
});

export default function (pi: ExtensionAPI) {
	/**
	 * Startup handshake.
	 *
	 * If this file fails to load, Pi does not refuse to start — it emits an
	 * `extension_error` and runs on with no gate, which is a silent fail-open.
	 * PiAdapter therefore waits for this notification before sending the first
	 * prompt, turning that into a startup error.
	 */
	pi.on("session_start", (_event, ctx) => {
		ctx.ui.notify(envelope("ready", { questionTool: QUESTION_TOOL }), "info");
	});

	/**
	 * The approval gate.
	 *
	 * Fires after `tool_execution_start` and before the tool runs, so by the
	 * time the adapter sees our dialog it has already seen that event and can
	 * recover the tool's structured arguments from it. That is why the envelope
	 * carries only the id and name.
	 */
	pi.on("tool_call", async (event, ctx) => {
		// Asking permission to ask the user a question would be circular.
		if (event.toolName === QUESTION_TOOL) return;
		if (READ_ONLY_TOOLS.has(event.toolName)) return;

		// No dialog transport (print/json mode) means nobody can approve, so
		// nothing runs. Every failure path below fails closed the same way.
		if (!ctx.hasUI) {
			return { block: true, reason: "No approval channel is attached to this session." };
		}

		const choice = await ctx.ui.select(
			envelope("approval", { toolCallId: event.toolCallId, toolName: event.toolName }),
			[ALLOW, DENY],
			// Aborting the turn resolves pending dialogs to undefined, which
			// lands in the deny branch and unwinds the tool cleanly.
			{ signal: ctx.signal },
		);
		if (choice === ALLOW) return;

		// The client sends its denial reason with the decision, so this second
		// round trip is protocol-only — the adapter answers it immediately with
		// what it already holds, and the user sees a single prompt. Keeping the
		// reason out of the `select` answer means that answer is always one of
		// the offered options.
		const reason = await ctx.ui.input(
			envelope("deny_reason", { toolCallId: event.toolCallId }),
			undefined,
			{ signal: ctx.signal },
		);

		const denialReason = reason?.trim();
		return {
			block: true,
			reason: denialReason
				? `Tool execution was denied by the user. Reason: ${denialReason}`
				: "Tool execution was denied by the user.",
		};
	});

	/**
	 * AskUserQuestion.
	 *
	 * The schema batches 1-4 questions into one call on purpose: a
	 * single-question tool invites the model to drip-feed questions across
	 * turns, and grouping them again downstream would need a timeout to know
	 * when a set is complete. One call means the adapter knows exactly how many
	 * dialogs belong together.
	 *
	 * The transport can only carry one question per dialog, so `execute` fans
	 * the batch out into concurrent `select` calls stamped with a shared
	 * `toolCallId`; Pi runs dialogs concurrently, and PiAdapter reassembles them
	 * into the single `question` event the clients already render.
	 */
	pi.registerTool({
		name: QUESTION_TOOL,
		label: "Ask the user",
		description:
			"Ask the user up to 4 multiple-choice questions and wait for their answers. " +
			"Use when a decision is genuinely the user's to make: one that cannot be " +
			"resolved from the request, the code, or a sensible default.",
		promptSnippet: "Ask the user multiple-choice questions when a decision is theirs to make",
		promptGuidelines: [
			`Use ${QUESTION_TOOL} when different readings of the request would lead to materially different work.`,
			"Ask everything you need in one call — up to 4 questions — rather than asking again on a later turn.",
			"Do not ask about choices with a conventional default, or facts you can verify by reading the code.",
			"Every option needs a label and a description explaining what picking it means.",
		],
		parameters: QUESTION_SCHEMA,
		// Failures are raised, not returned: Pi's AgentToolResult carries only
		// `content` and `details`, and its contract is to throw rather than
		// encode an error in the content.
		async execute(toolCallId, params, signal, _onUpdate, ctx) {
			const questions = normalizeQuestions(params?.questions);
			if (questions.length === 0) {
				throw new Error("No valid questions were supplied.");
			}
			if (!ctx.hasUI) {
				throw new Error("No question channel is attached to this session.");
			}

			const picks = await Promise.all(
				questions.map((question, index) =>
					ctx.ui.select(
						envelope("question", {
							toolCallId,
							index,
							count: questions.length,
							question,
						}),
						question.options.map((option) => option.label),
						{ signal },
					),
				),
			);

			// A pick is undefined when the client cancelled that dialog or the
			// turn was aborted. Report rather than inventing an answer, so the
			// model does not act on a choice the user never made.
			const answers: Record<string, string> = {};
			questions.forEach((question, index) => {
				const pick = picks[index];
				if (typeof pick === "string" && pick.length > 0) {
					answers[question.question] = pick;
				}
			});
			if (Object.keys(answers).length === 0) {
				throw new Error("The user did not answer.");
			}

			return {
				content: [{ type: "text" as const, text: JSON.stringify(answers) }],
				details: { answers },
			};
		},
	});
}
