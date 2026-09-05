# Ids, for client authors

`projects.id` and `sessions.id` are **JSON numbers**. They used to be uuid
strings. If you are writing or reviewing client code that compares, stores,
renders or routes on an id, read this first — the change is small enough to look
like a non-event and has a handful of ways to fail silently.

## What is a number and what is not

Numbers, from the server's own tables:

| Field | Where |
|---|---|
| `id` | `GET /projects`, `POST /projects` |
| `id` | `GET /sessions`, `POST /sessions`, `PATCH /sessions/{id}` |
| `project_id` | every session object |
| `session_id` | scrollback rows and WebSocket frames |

**Strings, and staying strings.** These are minted by an agent or by the
approval protocol, not by our database, and nothing here applies to them:

- `agent_session_id` — the selected agent harness's own resume id
- `request_id` — an approval or question request
- `option_id`, and the `id` inside an approval `options[]` entry — e.g.
  `"allow"`, `"deny"`

So an `id` field is not uniformly a number. `session.id` is; the `id` on an
approval option is not. Check which object you have.

## The five rules

**1. Never `===` an id against anything that has been through the DOM.**
`element.dataset.foo = 1` stores the *string* `"1"`, and reading it back gives
`"1"`. `1 === "1"` is `false`, so the comparison fails quietly — no error, just a
lookup that never matches. Same for `getAttribute`, `value`, and any id that has
been through a URL or a template literal. Coerce explicitly at that boundary:

```js
// wrong — dataset is always a string
const session = sessions.find((s) => s.id === node.dataset.id);
// right
const session = sessions.find((s) => String(s.id) === node.dataset.id);
```

**2. `Map` and `Set` keys are type-sensitive.** `new Set([1]).has("1")` is
`false`. If you build a `Set` from `sessions.map((s) => s.id)` and then test it
with an id that came from storage or markup, every test fails. Pick one type at
the point you build the collection and coerce everything entering it.

**3. No string methods on an id.** `id.slice(0, 8)` throws `TypeError: id.slice
is not a function` on a number. Truncating an id was a uuid-era habit and is now
pointless anyway — an integer id is already short. If you want a fallback label,
use `String(id)` or just render the id whole.

**4. Persisted ids do not survive the migration.** The move to integers
*renumbered* every row; it did not preserve the old values. Anything a client
saved before it — a localStorage tab list, a bookmarked URL, an Android
notification preference set — refers to ids that no longer exist. That is fine
as long as your restore path **fails closed**: check a restored id against the
current session list and drop it if absent, rather than assuming it resolves.
Do not write a migration for this; it self-corrects after one use.

**5. Ids are never reused.** The columns are `INTEGER PRIMARY KEY
AUTOINCREMENT`, not bare rowids, so deleting session 7 does not free the number
7 for the next session. A client holding a stale id gets a `404`, never a
different session wearing the same id. You can rely on this — a cached id is
either valid or gone, never silently someone else.

## Routing and validation

`session_id` is now typed `int` on every route (`/sessions/{id}`,
`/sessions/{id}/turn`, `/sessions/{id}/bash`, `/sessions/{id}/stop`,
`/ws/sessions/{id}`). Two consequences:

- A non-numeric path segment is a **422**, not a 404. FastAPI rejects it before
  the handler runs, so the body is a validation error, not the app's
  `{"detail": "Session not found"}`. A client that pattern-matches on 404 to
  mean "gone" should treat 422 as "my id is malformed" — a bug, not a
  missing session.
- Interpolating an id into a URL is still fine. `` `/sessions/${id}` `` works
  unchanged, because numbers stringify predictably.

The project endpoints are unaffected: they are addressed by `path` in the body,
not by id in the URL, and that has not changed.

## Client status

### Android (`agent-ui-android`) — works, but on an implicit conversion

Nothing here is broken. Ids are held as opaque `String`s throughout —
concatenated into URLs (`SessionActivity.java:489`), passed as Intent extras,
stored in `Prefs` as string sets — and nothing parses them numerically or slices
them. Keep it that way: an id is **identity, not arithmetic**. Never `parseInt`
one, never compare two with `<`.

The reason it still works is worth knowing, because it is invisible.
`Session.from` and `Project.from` read ids with `optString("id", "")`, and
org.json's `optString` quietly coerces a JSON number to its string form via
`String.valueOf`. So `1` becomes `"1"` with nothing in the code saying so. Three
sites rely on this: `Session.java:50`, `Session.java:52`, `Project.java:43`.

That is load-bearing behaviour resting on a library detail. Two things follow:

- **Making it explicit is worthwhile** — a small `Json.id(o, key)` helper used
  at those three sites, documenting that ids arrive as numbers and are held as
  strings. It would also pin the formatting: `String.valueOf` on a `Double`
  yields `"1.0"`, an id that matches nothing, whereas going via `longValue()`
  cannot. Not done at time of writing.
- **Do not extend the pattern blindly.** If you add another id-bearing field,
  convert deliberately rather than leaning on `optString` to guess.

Note this coercion is also what lets the app talk to a *pre-migration* server,
where ids are still uuid strings — any replacement should keep strings passing
through untouched.

Verifying the coercion claim needs a JVM; there was no Java toolchain on the
machine where this was written, so it is read from org.json's implementation
rather than observed. Worth one smoke test on a device.

### Desktop (`agent-ui-desktop`) — known-broken, unfixed at time of writing

Five sites assume string ids. If you are working nearby, fix them:

| File | Problem | Effect |
|---|---|---|
| `js/tabs.js:157` | `parsed.filter((v) => typeof v === 'string')` | Restored ids are numbers, so the filter discards all of them — **open tabs never restore**, silently and permanently |
| `js/app.js:334` | `known.has(id)` where `known` is a `Set` of numbers | Second, independent reason tab restore fails |
| `js/sidebar.js:60` | `node.dataset.id === id` | Active-session highlight never applies |
| `js/sidebar.js:148` | `find((s) => s.id === node.dataset.id)` | `refreshStatuses()` matches nothing; status dots stop updating |
| `js/sidebar.js:121`, `js/tabs.js:129` | `id.slice(0, 8)` | `TypeError` — latent, only on the `name ||` fallback, and the server requires a non-empty name |

Note how many of these fail *silently*. Nothing throws, nothing logs; a feature
just stops. When you touch id-handling code here, exercise it — open a session
from the sidebar, watch a status dot change, reload with tabs open — rather than
trusting that no error appeared in the console.

## The underlying rule

Treat an id as an **opaque token with a known type at each boundary**. It is a
number in JSON, a string in the DOM and in storage, and a string once it is in a
URL. Convert deliberately when it crosses, and never let an implicit conversion
decide for you — that is exactly the class of bug the table above is made of.
