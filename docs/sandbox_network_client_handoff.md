# Server and project sandbox network exceptions

Implement on a new branch in each client/API repository.

## API contract

- `GET /sandbox-network`
- `PATCH /sandbox-network` with a required `sandbox_network_allowlist` array.
- Both return `{"sandbox_network_allowlist": [{"ip": "100.64.0.10", "port": 443}]}`.
- PATCH replaces the entire list; `[]` clears it. Duplicates are collapsed in
  first-occurrence order. A missing array or malformed port gives HTTP 422;
  invalid IP/destination gives HTTP 400. Invalid updates leave the list unchanged.
- These endpoints edit only the server scope. Normal server authentication
  applies. No WebSocket settings event is emitted.
- Project responses include `sandbox_network_allowlist` for that project's own
  entries, not its effective union with server entries.
- `POST /projects` accepts an optional `sandbox_network_allowlist` (default `[]`).
  Repeated creation of an existing project leaves its settings unchanged.
- `PATCH /projects`, addressed by `path`, accepts `sandbox_network_allowlist`:
  omission leaves it unchanged, a supplied array replaces the project list,
  and `[]` clears it back to server inheritance. Validation matches server scope.
- Effective exceptions are the union of server and project entries, deduplicated
  by IP/port. Projects cannot remove inherited server exceptions. There is no
  per-session list; worktree sessions use their parent project's entries.
- Exact unicast IPv4 literals only; no hostnames, CIDRs, IPv6, loopback,
  unspecified, reserved, multicast, or `169.254.0.53` (sandbox DNS proxy).
- TCP only; integer ports 1–65535. Separate entries for HTTPS and SSH as needed.

## Client settings

Add a server settings editor next to the existing sandbox path configuration:
load the list, add/remove IP + port rows, save the complete list, and display API
errors normally. Use the server's returned list after saving. Explain that each
entry exposes only that TCP port, including when Gitea and agent-ui-server share
an IP. The changes apply to newly launched sandboxed turns for both agents;
running turns retain their rules. An empty server list means no server-level
exceptions; project-specific exceptions may still apply.

Add the same editor to project settings. Load/save the project's own list via
project GET/POST/PATCH models. Explain that server exceptions are inherited and
cannot be removed at project level. Empty means server inheritance only.

## Shared API repository

Add typed destination/request/response models and GET/PATCH client methods to
match this contract, including `sandbox_network_allowlist` on project response,
create, and update models; update contract documentation/tests. Coordinate method names
with desktop and Android implementers.

## Security/runtime

The server adds exact host routes and namespace-local nftables output filtering
before Bubblewrap drops capabilities. Other ports and UDP on exception IPs stay
blocked. Existing private/host blackholes, internet access, and DNS forwarding
remain. `nft` and kernel nftables support are required when the list is nonempty;
setup failures fail closed. No compatibility endpoints or bypass auto-approval.
