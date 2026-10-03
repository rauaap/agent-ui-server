# Server-wide sandbox network exceptions

Implement on a new branch in each client/API repository.

## API contract

- `GET /sandbox-network`
- `PATCH /sandbox-network` with a required `sandbox_network_allowlist` array.
- Both return `{"sandbox_network_allowlist": [{"ip": "100.64.0.10", "port": 443}]}`.
- PATCH replaces the entire list; `[]` clears it. Duplicates are collapsed in
  first-occurrence order. A missing array or malformed port gives HTTP 422;
  invalid IP/destination gives HTTP 400. Invalid updates leave the list unchanged.
- Normal server authentication applies. This is a server-wide setting, not a
  project or session field. No WebSocket settings event is emitted.
- Exact unicast IPv4 literals only; no hostnames, CIDRs, IPv6, loopback,
  unspecified, reserved, multicast, or `169.254.0.53` (sandbox DNS proxy).
- TCP only; integer ports 1–65535. Separate entries for HTTPS and SSH as needed.

## Client settings

Add a server settings editor next to the existing sandbox path configuration:
load the list, add/remove IP + port rows, save the complete list, and display API
errors normally. Use the server's returned list after saving. Explain that each
entry exposes only that TCP port, including when Gitea and agent-ui-server share
an IP. The changes apply to newly launched sandboxed turns for both agents;
running turns retain their rules. Empty means no private-network exceptions.

## Shared API repository

Add typed destination/request/response models and GET/PATCH client methods to
match this contract; update contract documentation/tests. Coordinate method names
with desktop and Android implementers.

## Security/runtime

The server adds exact host routes and namespace-local nftables output filtering
before Bubblewrap drops capabilities. Other ports and UDP on exception IPs stay
blocked. Existing private/host blackholes, internet access, and DNS forwarding
remain. `nft` and kernel nftables support are required when the list is nonempty;
setup failures fail closed. No compatibility endpoints or bypass auto-approval.
