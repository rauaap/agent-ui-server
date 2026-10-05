# Shared assets design

## Purpose

Let agents create HTML documents and supporting assets in ordinary filesystem directories and hand the owner links to them. The server serves registered directories directly: there is no copying, publishing action, or server-imposed project layout.

This document describes the agreed MVP, not an implemented feature.

## Separation of concerns

- **Serving:** a registered URL identifier maps to a filesystem directory.
- **Organization:** optional project association determines where clients display the setting.
- **Sandbox access:** existing session permissions determine which files agents can read or write. Registering a directory does not grant sandbox access.
- **Link resolution:** an agent tool translates a supplied filesystem path into a URL.

Several sessions or projects may write to the same directory. Project association is not an access boundary and does not constrain the directory location.

## Data model

```sql
CREATE TABLE shared_asset_roots (
    asset_root TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE
);
```

- `asset_root` is a user-chosen URL segment, such as `notes`. Validate it with an ASCII pattern such as `[A-Za-z0-9_-]+`.
- `path` is an absolute server-side directory path, stored in normalized form.
- `project_id = NULL` means global organization; otherwise the root appears under that project's settings.
- Overlapping directories are allowed. Duplicate normalized directory paths are rejected.
- Deleting a project unregisters its associated roots. Deleting or updating a root never deletes or moves files.
- Renaming an identifier is allowed and breaks existing links. No redirects or compatibility aliases.

Registration does not require the directory to exist. Persist registrations so they survive server restarts.

## Configuration API

These endpoints use existing API authentication.

### Create

```http
POST /shared-asset-roots
Content-Type: application/json

{
  "asset_root": "notes",
  "path": "/home/apoleon/notes",
  "project_id": null
}
```

Response: `201 Created`.

```json
{
  "asset_root": "notes",
  "path": "/home/apoleon/notes",
  "project_id": null,
  "url": "/shared-assets/notes/"
}
```

`project_id` defaults to `null`. Validate an explicitly supplied project ID against existing projects. Do not create the directory or test its existence/readability during registration.

### List

```http
GET /shared-asset-roots
```

Returns an array of root objects in the shape above. Clients can separate global and project-associated entries using `project_id`.

### Update

```http
PATCH /shared-asset-roots/{asset_root}
Content-Type: application/json

{
  "asset_root": "research",
  "path": "/home/apoleon/research",
  "project_id": 42
}
```

All fields are optional; omitted fields remain unchanged. Explicit `project_id: null` makes the root global. Neither `asset_root` nor `path` accepts null.

Response: `200 OK` with the updated root object and URL. Renaming removes the old URL immediately for subsequent requests.

### Delete

```http
DELETE /shared-asset-roots/{asset_root}
```

Response: `204 No Content`. Only the registration is removed.

### Configuration errors

- Unknown root or referenced project: `404`.
- Identifier or normalized directory already registered: `409`.
- Invalid identifier, non-absolute path, or invalid request fields: validation error (`422`).

## File serving

```http
GET  /shared-assets/{asset_root}/{file_path:path}
HEAD /shared-assets/{asset_root}/{file_path:path}
```

Examples:

```text
/shared-assets/notes/                  -> /home/apoleon/notes/index.html
/shared-assets/notes/report/           -> /home/apoleon/notes/report/index.html
/shared-assets/notes/report/chart.svg  -> /home/apoleon/notes/report/chart.svg
```

- Register the generic serving route at startup, before the existing `/` desktop static mount. Look up directory mappings at request time; do not dynamically append mounts.
- Support root URLs and trailing-slash directory URLs. Redirect directory requests lacking a trailing slash so relative asset links resolve correctly.
- Serve `index.html` for directories. Do not generate directory listings or add SPA fallback behavior.
- Unknown roots, absent directories, missing files, and directories without `index.html` return `404`.
- Serve normal content types and support HEAD without a response body.
- Configuration and filesystem changes take effect without restarting the server. Browser refresh is needed to see changes; no automatic live reload.
- Require cache revalidation, for example `Cache-Control: no-cache`, so ordinary reloads do not keep serving stale edits.
- Enforce containment within the registered directory. Reject traversal and symlinks escaping that directory; do not enable unrestricted symlink following.
- Ordinary filesystem/server failures surface normally. There is no recovery machinery for broken mounts or permissions.

## Authentication and HTML isolation

Asset GET/HEAD requests are unauthenticated. Existing API authentication remains unchanged. Existing host/network protections remain applicable.

Anyone who can reach the server can read files under registered directories. Root names and URLs are not secrets. Registration exposes the directory's files, not just documents linked from chat.

Send this header on shared-asset responses, including nested HTML documents:

```http
Content-Security-Policy: sandbox allow-scripts; object-src 'none'
```

This permits JavaScript while omitting `allow-same-origin`, giving documents an opaque origin. Scripts cannot read the application's browser storage or DOM. Forms, popups, downloads, and navigation of an enclosing application are restricted by the sandbox. The header applies when opening a document directly, not just inside a viewer. Plugin content is prohibited.

This policy is not a network firewall: document scripts can make network requests subject to browser rules. Do not grant authenticated API access through CORS to the opaque `null` origin. Existing API token requirements continue to protect API operations.

Sandboxed documents may have restrictions on browser APIs and cross-origin fetches, including fetching their own companion files through JavaScript. Ordinary document rendering and relative asset URLs remain the intended primary use case. No unrestricted interactive-app support is promised by the MVP.

## Agent tool: resolve_asset_link

```text
resolve_asset_link(path: string) -> { "url": string }
```

Example:

```text
Input:  /home/apoleon/notes/report/index.html
Output: { "url": "/shared-assets/notes/report/index.html" }
```

- Ungated and read-only.
- No discovery tool for MVP.
- No file-existence or session read-permission check. A resulting link may return `404`.
- The input must be an absolute filesystem path.
- Match against all registered roots, regardless of project association.
- When several roots contain the path, choose the deepest matching directory.
- Return an error when no registered root contains the supplied path.
- URL-encode the identifier and relative path while preserving path separators.
- Return a server-relative URL. Clients resolve it against their configured server address when opening it.

For overlapping roots:

```text
notes   -> /home/apoleon/notes
reports -> /home/apoleon/notes/reports

/home/apoleon/notes/reports/chart.svg
    -> /shared-assets/reports/chart.svg
```

Path matching must use component-aware containment (`relative_to()` / `is_relative_to()`), not string prefixes. Serving and resolution must agree on normalization and symlink containment. Non-strict path resolution can handle paths whose final components do not yet exist. Resolving a link does not create files, publish content, or grant access.

## Client behavior

- Server settings manage roots whose `project_id` is null.
- Project settings manage roots associated with that project.
- Agents return specific asset links in chat after creating files.
- Clients open relative links against the configured server address; Android chat links must be actionable.
- A root's Open action targets `/shared-assets/{asset_root}/`. Without an agent/user-authored `index.html`, that URL returns `404`.

No separate document editor, generated directory browser, publish workflow, root-discovery agent tool, or automatic agent permission changes are included.

## Precise path behavior

Use absolute paths only; do not expand `~` or environment variables. Normalize registration paths with `Path(path).resolve(strict=False)` and store that value. This resolves existing symlinks without requiring missing components to exist. Duplicate detection applies to the stored normalized path.

At serving and link-resolution time, resolve both the stored root and candidate path non-strictly again. This handles symlinks created after registration. Require component-aware containment of the resolved candidate within the resolved root. A symlink pointing outside the root cannot be served or linked through that root. Link resolution performs no `exists()`/`is_file()` check, although path resolution may consult filesystem metadata; resolution errors surface normally.

Choose the matching root with the most path components. If later filesystem changes cause two registrations to resolve to the same directory, use lexicographic `asset_root` order as a deterministic tie-breaker. URL-encode the resolved relative path. Filesystem symlink changes during a request must not allow containment checks to be bypassed; do not replace Starlette's static-file containment checks with a bare unchecked `FileResponse` join.

## Repository integration and client scope

- `src/agent_ui_server/main.py`: configuration routes, generic asset route, and placement before `mount_web_root(app)`.
- `src/agent_ui_server/network_guard.py`: authentication exemption through the existing `is_public` callback. Identify the actual shared-asset route and allow only GET/HEAD; do not use an unrestricted string-prefix exemption. Keep the desktop static exemption and host checks unchanged.
- `src/agent_ui_server/db.py`: persistence and project deletion. Connections already enable `PRAGMA foreign_keys = ON`; `delete_project()` ultimately deletes the project row, allowing the new `ON DELETE CASCADE` to remove its roots. Test the real project-delete endpoint, including its session teardown, rather than only direct SQL deletion.
- `src/agent_ui_server/agent.py`: both `ClaudeCodeAdapter` and `PiAdapter`, their server-side tool transport/dispatch, and tool registration.
- `src/agent_ui_server/pi_extension.ts`: Pi tool declaration and its approval hook. The new tool must execute without prompting.
- `src/agent_ui_server/session_tools.py`: existing shared tool validation/dispatch is a reference, but its inter-agent approval category must not be applied to this tool. Link resolution is not inter-agent communication.
- `src/agent_ui_server/tool_actions.py` and `actions.py`: inspect existing action classification/rendering conventions and represent the new tool consistently without introducing an approval requirement.

The end-to-end feature includes desktop and Android settings UI plus their shared API definitions in the sibling `agent-ui-api` repository, where applicable. Both clients should expose global and project root CRUD, including rename. Use existing settings/UI conventions; do not invent a new document editor.

Resolve chat link targets beginning with `/shared-assets/` against the configured server URL before opening them. A Markdown link such as `[Report](/shared-assets/notes/report/)` must open the right server in both clients. Android's parallel hyperlink-fix work should be inspected before making overlapping changes; generic clickable links alone may not supply server-relative URL resolution.

These sibling repositories may require separate write access or delegated sessions. A server-only implementation should report the remaining client work rather than claim the complete feature is done.

## Implementation notes

- Extend database persistence and configuration routes.
- Register asset serving before the desktop catch-all and integrate its GET/HEAD authentication exemption with the existing network guard.
- Apply CSP consistently to direct and nested assets.
- Add the resolve-link tool to both agent backends without approval gating.
- Add client settings and server-relative link handling.

Tests should cover CRUD, renaming, project deletion, missing directories, nested assets, index handling, HEAD, route precedence, CSP headers, existing API authentication, traversal, escaping symlinks, overlapping roots, URL encoding, and resolution of nonexistent files without sandbox read access.
