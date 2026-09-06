# Stunt Cookbook

Copy-paste recipes. Each snippet is a standalone rule unless noted.

## 0) One-Line Mocks

```yaml
defaults:
  host: "api\\.example\\.com"       # optional: scope every rule below to one host

mock:
  GET /api/users: [{id: 1, name: "Ada"}]
  POST /api/orders:
    status: 201
    body: {ok: true}
  /health: {status: "up"}
  /api/pdp: {file: "pdp.json"}
```

Keys are `[METHOD ]path`; the path matches exactly (query string ignored). A value is a
`respond_with` object only if every key is one of `status`/`body`/`file`/`headers`/`json`
*and* `status` is an integer — otherwise it is a bare JSON body, which is why
`/health` above returns `{"status": "up"}`. `mock:` entries compile to normal rules at
priority -1, so anything in `rules:` wins over them.

## 1) Fail First Two Requests, Then Succeed

```yaml
state:
  login_attempts: 0

rules:
  - name: "login-fail-1"
    url_contains: "/login"
    state:
      require:
        login_attempts: 0
      set:
        login_attempts: 1
    error:
      status: 429
      body: { "message": "Rate limited" }

  - name: "login-fail-2"
    url_contains: "/login"
    state:
      require:
        login_attempts: 1
      set:
        login_attempts: 2
    error:
      status: 429
      body: { "message": "Rate limited" }

  - name: "login-ok"
    url_contains: "/login"
    state:
      require:
        login_attempts: 2
    respond_with:
      status: 200
      body: { "ok": true }
```

## 2) Toggle Feature Flag on Each Call

```yaml
rules:
  - name: "feature-toggle"
    url_contains: "/feature"
    cycle:
      - status: 200
        body: { "enabled": false }
      - status: 200
        body: { "enabled": true }
```

## 3) Random 500s for Flaky Endpoints

```yaml
rules:
  - name: "flaky-health"
    url_contains: "/health"
    random:
      - status: 200
        body: { "ok": true }
      - status: 500
        body: { "ok": false }
```

## 4) Slow Down a Single Endpoint

```yaml
rules:
  - name: "slow-search"
    url_contains: "/search"
    delay:
      fixed: 1.2
```

## 5) Override Price by SKU

```yaml
rules:
  - name: "price-override"
    url_contains: "/cart"
    modify_response_json:
      set:
        $.items[?(@.sku=="ABC123")].price: 99.99
```

## 6) Remove PII from Responses

```yaml
rules:
  - name: "mask-email"
    url_contains: "/profile"
    modify_response_json:
      delete:
        - $.user.email
```

## 7) Inject Debug Header on Requests

```yaml
rules:
  - name: "debug-header"
    url_contains: "/api"
    modify_request_headers:
      set:
        X-Debug: "1"
```

## 8) Mock Response from File

```yaml
rules:
  - name: "mock-profile"
    url_contains: "/api/profile"
    respond_with:
      status: 200
      file: "sample_response.json"
      headers:
        Content-Type: "application/json"
```

## 9) Gate by State (Two-Step Checkout)

```yaml
state:
  stage: "start"

rules:
  - name: "checkout-start"
    url_contains: "/checkout"
    state:
      require:
        stage: "start"
      set:
        stage: "confirmed"
    respond_with:
      status: 200
      body: { "step": 1 }

  - name: "checkout-confirmed"
    url_contains: "/checkout"
    state:
      require:
        stage: "confirmed"
    respond_with:
      status: 200
      body: { "step": 2 }
```

## 10) Rewrite Request JSON Before It Reaches Backend

```yaml
rules:
  - name: "force-role"
    url_contains: "/user"
    modify_request_json:
      set:
        $.role: "admin"
```

## 11) Mock a Backend with No Proxy Config or CA Install (Reverse Mode)

For a mobile app or frontend that can just point its base URL at `localhost` — no system
proxy setting, no CA certificate to trust:

```yaml
mock:
  GET /api/status: {status: "ok"}
```

```bash
stunt --runner dump --mode reverse:https://api.example.com
curl http://localhost:8080/api/status   # -> {"status": "ok"}
```

Every other path is forwarded straight to `https://api.example.com`. In reverse mode the
upstream must be reachable (its responses are discarded, so it can return anything) —
mitmproxy dials it while setting up the flow, before any rule runs. That limit is specific
to reverse mode; in forward-proxy mode mocks need no backend at all. See
[Reverse-Proxy Mode](../README.md#reverse-proxy-mode) in the README.

## 12) Simulate a Flaky Mobile Connection

Latency + bandwidth + occasional drops. `global_delay` is the round-trip latency,
`throttle` is the pipe, and the rule drops one request in twenty:

```yaml
global_delay:
  random: [0.15, 0.6]
throttle: slow-3g          # 400 kbit/s ≈ 50 KB/s

rules:
  - name: "flaky link"
    path_contains: "/api/"
    probability: 0.05
    kill: true
```

Presets: `gprs` 50, `2g` 240, `slow-3g` 400, `3g` 1600, `dsl` 2000, `4g` 4000,
`wifi` 30000 (all kbit/s). Throttling is approximated as one `body_size / rate` sleep
before the whole body is handed over — enough to exercise spinners and timeouts, but not
packet-level pacing. Every transfer is capped at 30 s.

Throttle one endpoint instead of everything by putting `throttle:` on the rule:

```yaml
rules:
  - name: "the huge catalogue payload"
    path_contains: "/api/catalogue"
    throttle: 2g
```

## 13) Test Your Timeout Handling

Hang, then drop the connection. `delay` runs before the action, so this is exactly
"the server accepted my request and never answered":

```yaml
- name: "checkout hangs then dies"
  path_contains: "/api/checkout"
  delay: { fixed: 10 }
  kill: true
```

Raise `delay` above your client's timeout to assert the timeout path; lower it below to
assert the happy path still wins. `delay` is capped at 10 s — for a longer hang, use the
throttle cap instead (a large body at `gprs` holds the connection for 30 s).

## 14) Simulate the Network Dropping Mid-Request

`error:` returns a synthetic HTTP response, which is not the same failure at all. `kill`
drops the connection in the request phase, before the upstream is contacted, so the
client sees a transport error with no status code:

```yaml
- name: "network gone"
  host: "api\\.example\\.com"
  kill: true
```

Drop only the first request — the retry then succeeds against the real backend:

```yaml
- name: "one dropped request"
  path_contains: "/api/sync"
  once: true
  kill: true
```

Or take the whole API offline after a point in a flow, using state:

```yaml
state:
  online: true

rules:
  - name: "go offline once checkout starts"
    priority: 10
    path_contains: "/api/checkout"
    state:
      set: { online: false }

  - name: "everything fails while offline"
    path_contains: "/api/"
    state:
      require: { online: false }
    kill: true
```

## 15) Match a Body by Example, Without JSONPath

`json_contains` / `response_json_contains` take a *shape*, not a path expression: paste a
real request body, delete everything you don't care about, and what's left is the matcher.
Objects match when every key you kept is present with a matching value; arrays match when
each element you kept appears somewhere in the array. Extra keys and extra elements are
ignored, so this is a "contains", not an "equals".

```yaml
- name: "block admin bulk deletes"
  path_contains: "/api/bulk"
  json_contains:
    actor: { role: "admin" }
    operations:
      - { type: "delete" }        # anywhere in the operations array
  error:
    status: 403
    body: { error: "bulk delete disabled" }
```

For a leaf that varies, swap the value for a regex:

```yaml
- name: "flag internal callers"
  json_contains:
    user:
      email: { "$regex": "@corp\\.internal$" }
  modify_request_headers:
    set: { X-Internal: "1" }
```

Same thing on the way back — this one runs in the response phase, so the real backend is
called first:

```yaml
- name: "retry-able upstream error"
  response_json_contains:
    errors:
      - { code: "RATE_LIMITED" }
  delay: 2
```

`json_contains` sits alongside `json_body`; if you set both, both have to pass. Reach for
`json_body` when you need a path (`$.a.b[0].c`) or `{"$exists": false}`, and for
`json_contains` when you just have an example.

## 16) Patch One Array Element Out of a Hundred

`set` needs an index or a JSONPath filter, and both break the moment the backend reorders
its list. `merge` targets by *content* instead: `$where` is the same containment pattern
`json_contains` uses, and every element that contains it gets patched.

```yaml
- name: "make one product free"
  path_contains: "/api/cart"
  modify_response_json:
    merge:
      $.items:
        $where: { sku: "SKU-123" }
        $merge:
          price: 0
          badges: ["free"]
```

`$merge` is recursive, so nested objects keep their other keys — `{prefs: {theme: dark}}`
merged into `{prefs: {theme: light, lang: en}}` leaves `lang` alone. Scalars, lists and
mismatched types overwrite outright.

To swap an element out entirely rather than patch it, use `$replace`:

```yaml
- name: "replace the out-of-stock line"
  modify_response_json:
    merge:
      $.items:
        $where: { status: "OUT_OF_STOCK" }
        $replace: { sku: "PLACEHOLDER", status: "AVAILABLE", price: 999 }
```

Without `$where`, `merge` deep-merges straight into whatever the JSONPath selects — handy
for widening an object without listing every leaf as a separate `set`:

```yaml
- name: "upgrade the session"
  modify_response_json:
    merge:
      $.session:
        role: "admin"
        features: { beta: true }
```

Not implemented: `insert`, `move`, `negated` and `forall`. `merge` always applies to
*every* predicate match; deleting elements has a home in `delete:` with a JSONPath
filter, and a mock proxy should not be reordering arrays behind your back.

## 17) Record Your App's Real Traffic, Then Work Offline

The Charles/Fiddler workflow: capture once, edit later, develop on a plane.

**Step 1 — record.** Point your app at the proxy and use it normally.

```bash
stunt --runner dump --record ./rules.yaml \
    --record-host '^api\.example\.com$' --record-path '^/api/'
# exercise the app... then Ctrl-C
# stunt --record: wrote ./rules.yaml (7 mock entries, 1 rule(s))
# stunt --record: wrote 1 body file(s) to ./mocks
```

`--record-host` / `--record-path` are the `host` and `path_regex` matchers you
already know — without them you record every byte the proxy sees, which is noise.

**Step 2 — read what you got.** It's ordinary rules YAML:

```yaml
mock:
  GET /api/users/1: {id: 1, name: Ada}      # small JSON, inlined
  GET /api/report: {file: rec_get_api_report.json}   # >2 KiB, written to mocks/
rules:
- name: recorded GET api.example.com/api/legacy
  method: GET
  path_regex: ^/api/legacy(\?.*)?$
  respond_with: {status: 404, body: {error: gone}}   # non-200 needs a full rule
```

Only `Content-Type` is copied from the upstream response, so no `Authorization`,
`Cookie` or `Set-Cookie` is captured — but **bodies are verbatim**. Skim for tokens
and personal data before you commit this.

**Step 3 — replay with the backend gone.**

```bash
stunt --rules ./rules.yaml --mocks ./mocks
```

Every recorded endpoint now answers from disk. Edit a value, save, and hot reload
picks it up — that's the point of recording into rules rather than into a binary
capture file.

Recording never overwrites an existing rules file; pass `--record-force` when you
mean to re-record over one.

Recorded bodies are scrubbed by default: values under sensitive JSON keys
(`access_token`, `password`, `apiKey`, …), JWTs and long high-entropy strings
become `<redacted>`, and the shutdown report says how many were replaced.
`--record-raw` turns that off. Treat scrubbing as a safety net, not a guarantee —
it won't catch a secret that looks like an ordinary value, so still read the
recording before committing it.

## 18) Drive a Stateful Flow from the UI, Without Restarting

Recipe 9 gates step two behind state set by step one. Testing step two then means
replaying step one every time — and if you get the order wrong, restarting the
proxy to clear state. The mitmweb command palette skips both.

```yaml
state:
  checkout_step: 1

rules:
  - name: checkout-step-one
    path_contains: /checkout/start
    state:
      set: {checkout_step: 2}
    respond_with:
      status: 200
      body: {next: "/checkout/pay"}

  - name: checkout-step-two-fails
    path_contains: /checkout/pay
    state:
      require: {checkout_step: 2}
    error:
      status: 402
      body: {error: "card declined"}
```

Open mitmweb (it is what plain `stunt` launches), press `:` for the command
palette, and:

```
:stunt.rules.list
  Stunt: 2 rule(s), priority order:
    * checkout-step-one       prio=0    hits=0    active
    * checkout-step-two-fails prio=0    hits=0    gated (state.require)
```

`gated` is the answer to "why is my 402 not firing?" — the flow has not reached
step 2. Jump it there directly:

```
:stunt.state.set checkout_step 2
  Stunt state: checkout_step = 2
```

Now hit `/checkout/pay` in your app and you get the declined card, no start call
needed. `:stunt.state.get` shows where the flow currently sits, and
`:stunt.reload` puts everything back to the `state:` block in the file.

Two things worth knowing:

- The value is parsed as JSON when it parses, so `2` matches `require: {step: 2}`
  (the number) rather than `"2"` (the string). Quote it — `:stunt.state.set
  tier "gold"` — when you actually want a string that looks like a number.
- `:stunt.rules.toggle checkout-step-one` silences the first rule so it stops
  resetting the step under you. That toggle is in memory only: the next reload of
  `rules.yaml` brings the rule back.
