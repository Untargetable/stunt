# Stunt

**An understudy for your backend.**

Stunt is a mitmproxy addon for local frontend and mobile testing. It lets you mock, modify, and delay HTTP/HTTPS traffic using a simple `rules.yaml`.
The goal is a low-skill-floor tool that scales from quick UI checks to advanced, rule-driven network simulation.

- Fast UI iteration without a backend.
- Deterministic mocking and targeted JSON mutations.
- Works for web, iOS, and Android.
- Hot reload of rules and mock files.
- Simple YAML for juniors, powerful matching for seniors.

## Install

Requires Python 3.12 or newer.

```bash
pipx install stunt        # recommended: isolated, still on your PATH
uv tool install stunt     # same idea, if you use uv
pip install stunt         # or into a venv you manage yourself
```

`uvx stunt` runs it without installing anything.

Any of these puts a `stunt` command on your `PATH` for the rest of this README.

<details>
<summary>From a clone (for developing Stunt itself)</summary>

```bash
git clone https://github.com/Untargetable/stunt.git
cd stunt
python -m venv venv
source venv/bin/activate   # see Contributing below for other shells
pip install -e .
```

Venv activation for other shells and running the test suite are covered in
[Contributing / Development](#contributing--development).
</details>

## Your First Mock in 60 Seconds

Create a `rules.yaml` in an empty directory:

```yaml
mock:
  /api/status: {status: "ok"}
```

That one line mocks `GET /api/status` on **any** host with a `200 application/json`
response — no backend required. (`stunt init` scaffolds this file for you, plus a
`mocks/` folder with an example — see [`stunt init`](#stunt-init) below.)

Start the proxy:

```bash
stunt --runner dump
```

This listens on port `8080` on **all interfaces** (`0.0.0.0`), which is what lets a phone
on the same Wi-Fi reach it — and also means anyone on your network can. On an untrusted
network, restrict it with `--listen-host=127.0.0.1`. Before pointing any real client at it,
trust its certificate once: with the proxy running, open `http://mitm.it` **through
it** (e.g. `curl -x http://127.0.0.1:8080 http://mitm.it` or a browser configured to
use the proxy — see [Certificate & Client Setup](#certificate--client-setup) for every
platform). Skipping this makes every HTTPS request fail with a certificate error.

Now verify the mock fires, with no backend running at all:

```bash
curl -x http://127.0.0.1:8080 http://api.example.com/api/status
# {"status": "ok"}
```

That's it — that's the loop. Edit `rules.yaml`, save, and the running proxy
hot-reloads it; no restart needed.

Each `mock:` key is `[METHOD ]path`. The method is optional; without it every method
matches. The path is matched **exactly** (a query string is ignored, so
`/api/users?page=2` still matches, but `/api/users/1` does not).

Each value is either a **bare body** — any JSON object, array or scalar, served as
`200 application/json` — or a **`respond_with` object** when you need to set the
status, headers, or a mock file:

```yaml
mock:
  GET /api/users: {id: 1, name: "Ada"}          # bare body
  POST /api/orders:                             # respond_with object
    status: 201
    body: {ok: true}
  /health: {status: "up"}                       # bare body (see below)
  /pdp: {file: "pdp.json"}                      # served from mocks/pdp.json
```

**How the two are told apart:** a value is a `respond_with` object only if it is a
non-empty map whose keys are *all* from `status`, `body`, `file`, `headers`, `json`
**and** whose `status`, if present, is an integer. Everything else is a bare body.
So `{status: "up"}` is a health-check *body*, and `{status: 201, id: 5}` is a body too
(`id` is not a `respond_with` key). If you ever want the strict reading, be explicit:
`{body: {status: 201, id: 5}}`.

When you need matchers, state, delays, cycling responses or JSON mutation, graduate to
the full [`rules:`](#full-option-reference) form — `mock:` desugars into exactly those
rules, so nothing behaves differently.

## Certificate & Client Setup

Every client needs the mitmproxy CA certificate trusted once, and its proxy settings
pointed at wherever Stunt is running (port `8080`, all interfaces — use your
machine's LAN IP from a phone, `127.0.0.1` from the same machine).

### Web: Chrome / Edge
1. Open system proxy settings.
2. Set HTTP/HTTPS proxy to the host/port where Stunt is running (default `127.0.0.1:8080`).
3. Visit `http://mitm.it` in the browser to install the mitmproxy certificate.

### Web: Firefox
1. Settings → Network Settings → Manual proxy.
2. Same host/port as Stunt.
3. Install cert from `http://mitm.it` or import into Firefox certificate store.

### iOS
1. Ensure phone and laptop are on the same Wi-Fi.
2. On iOS: Wi-Fi → (i) → Configure Proxy → Manual.
3. Set server to your laptop IP and port to Stunt port (default 8080).
4. Open Safari to `http://mitm.it` and install the iOS certificate.
5. Enable full trust: Settings → General → About → Certificate Trust Settings.

### Android
1. Ensure device and laptop are on the same Wi-Fi.
2. Android Wi-Fi → Edit network → Proxy → Manual.
3. Set host to your laptop IP and port to Stunt port.
4. Open browser to `http://mitm.it` and install the cert (user CA).

Note: Some apps use certificate pinning. In those cases, either disable pinning in your dev build or use a debug build that trusts user CAs.

## Everyday Recipes

More copy-paste recipes live in [`docs/cookbook.md`](docs/cookbook.md). A few common
ones to get started:

### Simulate a slow API
```yaml
- name: "slow search"
  url_contains: "/api/search"
  delay:
    random: [0.3, 1.5]
```

### Force a 500
```yaml
- name: "simulate outage"
  url_contains: "/api/heavy"
  error:
    status: 500
    body: { "message": "Internal error" }
```

### Mock an endpoint whose backend is down
`respond_with` (and the `mock:` shorthand) is served in the request phase, so it never
touches the network — the mock fires even with no upstream running at all:
```yaml
- name: "profile from file"
  url_contains: "/api/profile"
  respond_with:
    file: "sample_response.json"
    headers:
      Content-Type: "application/json"
```

### Simulate a slow mobile connection
`delay` adds latency; `throttle` adds *transfer time*, so a big payload stops arriving
instantly. Together they model a real connection:
```yaml
global_delay: 0.2      # latency, applies to every request
throttle: slow-3g      # bandwidth, applies to every response body
```

### Simulate the network dropping
`kill: true` drops the connection in the request phase — the client sees a broken
connection, not an HTTP status code:
```yaml
- name: "offline"
  url_contains: "/api/"
  kill: true
```

### Test your timeout handling
Hang for 10 seconds, then drop — `delay` runs before the action, so a timeout is just
`delay` + `kill`:
```yaml
- name: "hang then drop"
  url_contains: "/api/checkout"
  delay: { fixed: 10 }
  kill: true
```

### Modify one field of a real response
This fetches the real response first, then edits it — the upstream must be reachable:
```yaml
- name: "patch one field"
  url_contains: "/api/profile"
  modify_response_json:
    set:
      $.betaFlag: true
```

## Tools

### `stunt init`

Scaffolds a working setup in the current directory: `rules.yaml` (with the schema
modeline and a couple of `mock:` shorthand entries) plus `mocks/` with one example
mock file. This is the intended way to create `mocks/`; the addon never creates it
speculatively.

```bash
stunt init          # creates rules.yaml + mocks/, skips files that already exist
stunt init --force  # overwrites them instead
```

It prints what it created or skipped, and a short next-step hint for starting the
proxy and trusting the mitmproxy CA cert.

### `stunt lint`

Validates a rules file without starting a proxy — good for CI or a pre-commit hook.
It reuses the exact same YAML parsing and rule-compiling code path the live addon
uses, so lint results and runtime behaviour never drift apart.

```bash
stunt lint                              # discovers rules.yaml the same way the proxy does
stunt lint --rules ./rules.yaml --mocks ./mocks
```

It reports, and exits non-zero on:
- YAML syntax errors, with line/column.
- Unknown keys, with near-miss suggestions (e.g. `respnd_with` → did you mean `respond_with`?).
- Rules with no recognised action (a typo'd action key silently disables the rule).
- Rules with **no matchers at all** — these match every request, which is almost
  always a mistake.
- `respond_with`/`error`/`cycle`/`random` entries whose `file:` mock doesn't exist.

Exits `0` with an `OK: <path>` summary line when the file is clean.

### Record real traffic (`--record`)

Run your app once against the real backend with recording on, stop the proxy, and
you have a `rules.yaml` that replays those responses with the backend switched off.

```bash
stunt --runner dump --record ./rules.yaml \
    --record-host '^api\.example\.com$' \
    --record-path '^/api/'
# ... exercise your app, then Ctrl-C
# stunt --record: wrote ./rules.yaml (7 mock entries, 2 rule(s))
# stunt --record: wrote 2 body file(s) to ./mocks
```

| Flag | Effect |
|---|---|
| `--record PATH` | Record this session and write it to `PATH` on shutdown. Off unless given. |
| `--record-host REGEX` | Only record hosts matching this regex — same dialect as the `host` matcher. |
| `--record-path REGEX` | Only record paths matching this regex — same dialect as `path_regex`. |
| `--record-force` | Allow `--record` to overwrite an existing rules file. |
| `--record-raw` | Turn secret scrubbing **off** and record bodies verbatim. |

Recording is **CLI-only and off by default**. There is no `record:` key in
`rules.yaml`: recording *writes* a rules file, so a switch living inside one would
re-arm itself on hot reload and overwrite the file it was read from.

What you get:

- **First response per `METHOD + path` wins** — a replay reproduces what your app
  saw first, not the last cache-buster.
- **`mock:` shorthand** for the ordinary case (200, JSON, single host). Anything
  needing more — non-200 status, a custom `Content-Type`, several hosts — becomes a
  full `rules:` entry with `method`/`path_regex` (and `host`) matchers.
- **Bodies**: JSON bodies of **2048 bytes or less** are inlined into the rules file;
  bigger ones, and anything that isn't JSON, are written to `mocks/` and referenced
  with `file:`. 2 KiB is roughly the largest blob that still reads and diffs like
  config rather than like data.
- **Headers**: only `Content-Type` is carried over. That's an allowlist, so
  hop-by-hop headers and `Authorization`/`Cookie`/`Set-Cookie` are skipped by
  construction.
- **Scrubbing (on by default)**: recorded response bodies are scanned before
  anything is written. Values under sensitive JSON keys (`access_token`,
  `password`, `apiKey`, `sessionId`, …), JWTs anywhere in a body, and long
  high-entropy opaque strings are replaced with `<redacted>`. JSON is walked by
  key; other bodies get a regex pass. The header of the generated file and the
  shutdown report both say how many values were replaced. `--record-raw` disables
  it when you genuinely need the real payload.
- **No clobbering**: an existing target file aborts recording at startup unless you
  pass `--record-force`, and the run tells you exactly what was written.
- Responses Stunt itself served are never recorded — only real upstream traffic.

> **Review recordings before committing them.** Scrubbing is a best-effort safety
> net against accidental commits, not a DLP tool: it knows common token shapes and
> key names, and it deliberately leaves ordinary-looking values alone. Emails, order
> IDs, names and any secret that doesn't look like one still land in `rules.yaml`
> and `mocks/`. The output is plain YAML — read it, trim it.

The result passes `stunt lint` and validates against
`docs/stunt_schema.json` as-is.

### Match-trace log (`--trace-matches`)

The most common question when mocking traffic: "why didn't my rule fire?" Opt in
with `--trace-matches` on the CLI, or `--set stunt_trace=true`, or a top-level
`trace: true` in `rules.yaml`. It costs nothing when off — the check is a single
attribute read on the hot path.

```bash
stunt --runner dump --trace-matches
```

Per request, it logs one block naming every rule considered and, for each one that
didn't match, which specific condition rejected it:

```
Stunt trace [request] GET /api/x
  rule 'users-get': path_regex did not match '/api/x'
  rule 'orders-post': method 'GET' not in ['POST']
  no rule matched — falling through to the real backend
```

A matching rule shows what it did instead (`MATCHED -> served mock response`, etc.).
Trace output respects `quiet: true` — a quiet addon stays quiet even with tracing on.

### Commands in the mitmweb UI

Stunt registers commands with mitmproxy, so its rules stop being invisible
while traffic scrolls past. They work in every runner:

- **mitmweb** (the default) — hit `:` to open the command palette, then start
  typing `stunt`. Results are printed in the palette.
- **mitmproxy console** (`--runner proxy`) — same thing, `:` then the command name.
- **HTTP** — mitmweb serves `GET /commands` and `POST /commands/<name>`, so a
  script can drive them too.

| Command | What it does |
| --- | --- |
| `stunt.rules.list` | Every loaded rule in priority order, with its hit count and whether it is `active`, `exhausted` (a spent `once`/`count`), `gated` (its `state.require` is not satisfied), or disabled |
| `stunt.rules.toggle <name>` | Turn one rule off (or back on) **for this session only** |
| `stunt.state.get` | Dump the current state dict |
| `stunt.state.set <key> <value>` | Set one state key. The value is parsed as JSON when it parses (`2` is the number 2, `true` a bool, `"x"` a string), otherwise kept as a plain string |
| `stunt.record.start <out> [host] [path]` | Arm recording to a rules file, with the optional `host`/`path` regex filters `--record-host`/`--record-path` take |
| `stunt.record.stop` | Stop recording and write the file now, without shutting the proxy down |
| `stunt.reload` | Force a reload of `rules.yaml` even though the file has not changed |

Sample output:

```
Stunt: 2 rule(s), priority order:
  * flaky-checkout  prio=10   hits=1    exhausted (once)
  - step-two        prio=0    hits=0    DISABLED (session)
```

**`stunt.rules.toggle` is in-memory only.** It never edits `rules.yaml`, and
because `rules.yaml` is the hot-reloading source of truth, the *next reload wipes
every toggle* — saving the file, `stunt.reload`, or touching a mock all restore
the rule. Use it to answer "is this rule the one breaking my page?" in a single
click; use the file for anything you want to keep.

Reload also resets state, hit counts and cycle positions, so a `stunt.state.set`
is equally session-scoped.

## Full Reference

The sections below are reference material, not a tutorial — see
[Your First Mock in 60 Seconds](#your-first-mock-in-60-seconds) and
[Everyday Recipes](#everyday-recipes) for the narrative path.

### Choosing the Runner

`stunt` defaults to `mitmweb` (web UI). You can switch runners:

```bash
stunt --runner web    # mitmweb (default)
stunt --runner proxy  # mitmproxy (terminal UI)
stunt --runner dump   # mitmdump (headless)
```

`--mode` is a deprecated alias for `--runner` (still works, prints a warning) — it was
renamed because mitmproxy has its own `--mode` flag (for `reverse:URL`, `transparent`,
`socks5`, `upstream:URL`; see [Reverse-Proxy Mode](#reverse-proxy-mode)), and the old name
shadowed it. Any `--mode` value that isn't `web`/`proxy`/`dump` is assumed to be mitmproxy's
own and forwarded straight through.

You can also pass any mitmproxy arguments after these options:

```bash
stunt --runner web --listen-host 0.0.0.0 --listen-port 8080
```

Use `--rules` / `--mocks` to point at a specific config instead of relying on
discovery (see [Locating `rules.yaml` and `mocks/`](#locating-rulesyaml-and-mocks)):

```bash
stunt --rules ./rules.yaml --mocks ./mocks
```

### Reverse-Proxy Mode

Regular mode (the default) is a forward proxy: the client has to be configured to use
`127.0.0.1:8080` as its proxy, and it has to trust Stunt's CA certificate for HTTPS.
That's fine for a browser or curl, but it's friction for a mobile app or a frontend that
can't easily be pointed at a system proxy.

**Reverse mode** flips this: Stunt listens like an ordinary HTTP server and forwards
every request to one fixed backend, mocking whatever your rules match along the way. The
client just changes its base URL to `http://localhost:PORT` — no proxy setting, no CA
install. This is the lowest-friction option when you can change a base URL (mobile apps,
frontends hitting an API base URL, CI jobs) and want mocks without touching client config.

```bash
stunt --runner dump --mode reverse:https://api.example.com
```

Now `http://localhost:8080/anything` is forwarded to `https://api.example.com/anything`
unless a rule intercepts it first — including the `mock:` shorthand, unchanged:

```yaml
mock:
  GET /api/status: {status: "ok"}
```

```bash
curl http://localhost:8080/api/status   # -> {"status": "ok"}, no -x, no CA needed
```

One caveat, specific to reverse mode: **the upstream must be reachable**, even though its
responses are discarded. mitmproxy dials the upstream while setting up a reverse-mode flow,
so if nothing is listening there the connection fails before any Stunt rule runs. This
affects every rule type equally — `respond_with`, `mock:` and `error` alike. Point
`reverse:` at any listening port (it can return garbage); the mock still wins.

In normal forward-proxy mode there is no such limitation: mocks are served in the request
phase and short-circuit before the network, so a rule serves fine with **no backend at all**.

`--mode` here is mitmproxy's own flag, not Stunt's runner selector — see
[Choosing the Runner](#choosing-the-runner) for why they're separate flags.

### Config Files

- `rules.yaml` controls global settings and rules.
- `mocks/` stores response bodies (JSON, text, fixtures).
- JSON schema is in `docs/stunt_schema.json` for editor validation.

The repo includes a full reference `rules.yaml` with a disabled kitchen-sink rule and practical examples. See `rules.yaml`.

#### Editor autocomplete and typo checking

Files written by `stunt init` and `stunt --record` start with a modeline pointing
editors at the schema:

```yaml
# yaml-language-server: $schema=https://raw.githubusercontent.com/Untargetable/stunt/main/docs/stunt_schema.json
```

With the [YAML extension](https://marketplace.visualstudio.com/items?itemName=redhat.vscode-yaml) in
VS Code, or the built-in JSON Schema support in JetBrains IDEs, this gives you
autocomplete for every rule key and flags anything the schema doesn't recognise —
including a typo like `path_regexp` or `respond:` — directly in the editor, before
you ever start the proxy.

Working inside a clone of this repo, you can point at the local copy instead, which
is what this repo's own `rules.yaml` does:

```yaml
# yaml-language-server: $schema=./docs/stunt_schema.json
```

#### Locating `rules.yaml` and `mocks/`

By default `stunt` looks for `rules.yaml` (and creates/uses `mocks/` next to it)
by checking, in order: `$STUNT_HOME`, the parent of `$VIRTUAL_ENV`, then walking
up from the current directory and from the addon's own location for a folder that
looks like a project root. If nothing is found, it warns on startup and loads
zero rules rather than failing silently.

To be explicit instead of relying on discovery:

```bash
stunt --rules /path/to/rules.yaml --mocks /path/to/mocks
# or
export STUNT_HOME=/path/to/project   # rules.yaml and mocks/ live here
stunt
```

`--rules` / `--mocks` always win over `$STUNT_HOME` and the discovery walk-up.

### Full Option Reference

#### Top-level

- `quiet` (bool): suppress rule logs (errors still logged).
- `trace` (bool): opt-in match-trace log — see [Match-trace log](#match-trace-log---trace-matches). Same effect as `--trace-matches`.
- `global_delay` (number | { random: [min, max] | number }): delay before processing each request.
- `throttle` (preset string | number | { kbps: number }): bandwidth throttling applied
  to every response body — see [Network Conditions](#network-conditions-throttle--kill).
- `state` (object): initial state values for stateful rules.
- `mock` (object): shorthand mocks keyed by `[METHOD ]path` — see [Your First Mock in 60 Seconds](#your-first-mock-in-60-seconds).
  Desugars into normal rules at **priority -1**, so an explicit `rules:` entry (default
  priority 0) always wins when both could match. `mock:` and `rules:` can be used together.
- `defaults` (object): rule keys shallow-merged into every rule, including the ones
  `mock:` generates. A rule's own key always wins. Useful for the keys you would
  otherwise repeat on every rule: `host`, `method`, `header_contains`,
  `query_contains`, `passthrough`, `priority`, `delay`, `quiet`, `max_body_size`.
  Avoid defaulting per-rule identity or actions (`name`, `respond_with`, `once`,
  `count`, `state`).
- `rules` (array): list of rule objects — the full form, with matchers, state,
  delays and mutations.

```yaml
defaults:
  host: "api\\.example\\.com"     # every rule below is scoped to this host
mock:
  /api/users: {id: 1}
rules:
  - name: "explicit wins"
    path_contains: "/api/users"
    respond_with: {status: 200, body: {from: "rules"}}
```

#### Rule fields

Core:
- `name` (string): rule name for logs.
- `enabled` (bool): enable/disable the rule.
- `priority` (int): higher runs first.
- `probability` (0..1): random execution chance.
- `count` (int): max number of times to apply.
- `once` (bool): apply at most once.
- `quiet` (bool): override global quiet per rule.
- `state`:
  - `require` (object): key/value matchers; supports `{ "$exists": bool }`.
  - `set` (object): sets state keys on rule hit.
  - `delete` ([string]): removes state keys on rule hit.
  - **Shorthand:** a rule-level `state:` given as a bare object with none of
    `require`/`set`/`delete` as keys is treated as `state.require` (a plain map of
    key/value matchers). This is the *opposite* of top-level `state:`, which sets
    *initial* values. Example: `state: { stage: "start" }` on a rule means "require
    `stage == start` to match" — it does not set `stage`.
- `delay`:
  - `fixed` (number): delay in seconds.
  - `random` ([min, max] | number): delay range in seconds, or a single number for a
    fixed-width "random" delay of that value.
  - If both `fixed` and `random` are set, `fixed` silently wins and `random` is ignored.
  - Values are clamped to the 0-10s range. Clamping logs a warning naming the rule
    and both the requested and the applied value.
- `throttle` (preset string | number | { kbps: number }): bandwidth for this rule's
  flow, overriding the top-level `throttle` — see
  [Network Conditions](#network-conditions-throttle--kill).
- `cycle` ([respond_with]): cycle through multiple responses on each match.
- `random` ([respond_with]): choose a response at random on each match.
- `passthrough` (bool): fetch the real response first, then serve `respond_with`/`cycle`/
  `random` in the response phase instead of short-circuiting the request.

Matchers:
- `method` (string | [string]): HTTP method(s).
- `host` (regex string): matches host.
- `url_contains` (string)
- `url_regex` (regex string)
- `path_contains` (string)
- `path_regex` (regex string)
- `status_code` (int | [int])
- `header_contains` ({ header: substring })
- `query_contains` ({ key: substring })
- `json_body` ({ jsonpath: value | {"$exists": bool} })
- `json_contains` (object | array): request body must *contain* this shape — recursive
  subset. Objects match when every key/value in the pattern is present and matches;
  arrays match when every pattern element is contained somewhere in the target array;
  extra keys and extra elements in the body are ignored. A leaf of `{"$regex": "..."}`
  matches by `re.search` instead of by equality. No JSONPath needed: paste an example
  body and trim it down. Combines with `json_body` (both must pass).
- `response_json` ({ jsonpath: value | {"$exists": bool} })
- `response_json_contains` (object | array): same containment semantics, against the
  response body. Like `response_json`, it forces the rule onto the response phase.

Actions:
- `respond_with`:
  - `status` (int)
  - `body` (any)
  - `file` (string): load from `mocks/`
  - `json` (bool)
  - `headers` (object)
- `error`:
  - `status` (int)
  - `body` (any)
  - `file` (string)
  - `headers` (object)
- `modify_request_headers`:
  - `set` (object)
  - `remove` ([string])
- `modify_request_query`:
  - `set` (object)
  - `remove` ([string])
- `modify_request_json`:
  - `set` (object)
  - `append` (object)
  - `delete` ([jsonpath])
  - `merge` ({ jsonpath: spec }): recursive deep merge. Objects merge key by key,
    anything else overwrites. A spec that is a plain object is merged into every match.
    A spec containing `$where` treats the match as an array and acts on every element
    that *contains* the `$where` shape (same predicate as `json_contains`), applying
    either `$merge` (deep merge into the element) or `$replace` (swap the whole element
    out). Not implemented: `insert`, `move`, `delete`, `negated`, `forall` — merging or
    replacing every predicate match covers the cases those exist for; delete already has
    its own key with JSONPath filters.
- `modify_response_headers`:
  - `set` (object)
  - `remove` ([string])
- `modify_response_json`:
  - `set` (object)
  - `append` (object)
  - `delete` ([jsonpath])
  - `merge` ({ jsonpath: spec }): recursive deep merge. Objects merge key by key,
    anything else overwrites. A spec that is a plain object is merged into every match.
    A spec containing `$where` treats the match as an array and acts on every element
    that *contains* the `$where` shape (same predicate as `json_contains`), applying
    either `$merge` (deep merge into the element) or `$replace` (swap the whole element
    out). Not implemented: `insert`, `move`, `delete`, `negated`, `forall` — merging or
    replacing every predicate match covers the cases those exist for; delete already has
    its own key with JSONPath filters.
- `kill` (bool): drop the connection instead of answering. Request phase only — the
  upstream is never contacted. See [Network Conditions](#network-conditions-throttle--kill).
- `max_body_size` (int): max JSON body size in bytes for parsing.

Behavior notes:
- The first matching rule applies and stops processing further rules.
- `respond_with` / `error` take precedence when matched.
- `cycle` / `random` override `respond_with`.
- `respond_with` / `cycle` / `random` are served in the request phase, so they work with
  no upstream at all and never reach the real backend. A rule matching on `status_code`
  or `response_json` needs the real response first and is therefore served in the
  response phase; `passthrough: true` asks for that explicitly.
- A rule with no recognised action is reported at load time and ignored. So is a
  request-phase action (`error`, `kill`, `modify_request_*`) on a rule that matches with
  `status_code` / `response_json*` — those matchers need a response, which is a phase
  where those actions never run.
- JSON modifications only apply to valid JSON bodies within `max_body_size`. A body that
  is not JSON makes the modification a no-op; the rule has still matched, so processing
  stops there rather than falling through to a lower-priority rule.
- A delay-only or state-only rule is still a match: it applies and stops the scan. Put a
  "slow this endpoint down" rule *after* the rule that mocks the same path, or give the
  mock the higher `priority`, or the mock never runs.
- `delay` is applied before `once` / `count` budgets are claimed, so under concurrency
  every racing flow waits and only the winners are served by that rule.
- State resets on `rules.yaml` reload and can be initialized via top-level `state`.
- `kill` runs before `error` and `respond_with`; a killed flow has no response at all.
- `throttle` is charged once per flow, on whichever phase first has a response body.

### Network Conditions (`throttle` / `kill`)

`delay` models **latency** — a fixed wait before anything happens. It does not model
**bandwidth**: a 5 MB payload still arrives in one piece. `throttle` fills that gap, and
`kill` covers the failures no HTTP status code can express.

#### `throttle` — bandwidth

Set it at the top level (every flow) or per rule (that flow only; a rule's own value
wins):

```yaml
throttle: 3g                     # preset
# throttle: { kbps: 256 }        # explicit rate in kbit/s
# throttle: 256                  # same thing, shorthand

rules:
  - name: "slow image endpoint"
    path_contains: "/images/"
    throttle: gprs
```

Presets, in kbit/s — the numbers line up with what Chrome DevTools and Charles use:

| Preset | kbit/s | ≈ bytes/s | 1 MB takes |
|---|---|---|---|
| `gprs` | 50 | 6,250 | 168 s (capped at 30 s) |
| `2g` | 240 | 30,000 | 35 s (capped at 30 s) |
| `slow-3g` | 400 | 50,000 | 21 s |
| `3g` | 1600 | 200,000 | 5.2 s |
| `dsl` | 2000 | 250,000 | 4.2 s |
| `4g` | 4000 | 500,000 | 2.1 s |
| `wifi` | 30000 | 3,750,000 | 0.3 s |

**How it is approximated.** Stunt computes `len(body) / rate` and sleeps once, then
hands the whole body over. It is *not* packet-level pacing: the client sees nothing,
then everything. That is enough to exercise spinners, timeouts and progress states, but
it will not reproduce chunked-streaming or first-byte behaviour.

Other guarantees:
- Charged **once per flow**. A mock served in the request phase is throttled there; a
  real response is throttled in the response phase. Never both.
- Composes with `delay`: total wait is latency + transfer time.
- Capped at **30 s** per transfer, so a typo (`kbps: 0.01`) cannot hang the proxy. When
  the cap bites, a warning names the body size and the uncapped duration.
- An unknown preset or a non-positive rate logs a warning and is ignored.

#### `kill` — connection-level failure

`error:` returns a synthetic HTTP *response*. Sometimes the thing you need to test is
the absence of one: a dropped connection, an offline network, a request that hangs and
then dies. `kill: true` calls mitmproxy's `flow.kill()` in the **request phase**, so the
upstream is never contacted and the client gets a transport-level failure.

```yaml
rules:
  # the network is gone
  - name: "offline"
    host: "api\\.example\\.com"
    kill: true

  # a request that hangs, then dies — your timeout handling under test
  - name: "gateway timeout"
    path_contains: "/api/checkout"
    delay: { fixed: 10 }
    kill: true

  # flaky: one request in five drops
  - name: "flaky link"
    path_contains: "/api/"
    probability: 0.2
    kill: true
```

`kill` is deliberately a plain boolean rather than a variant of `error:` — `error:`
always produces a response, and the whole point here is that there is not one. The
"timeout" case needs no key of its own: `delay` already runs before the action, so
`delay` + `kill` *is* hang-then-drop.

### Stateful Flows (State + Cycle/Random)

#### Require and Set State

```yaml
state:
  stage: "start"

rules:
  - name: "step-1"
    url_contains: "/checkout"
    state:
      require:
        stage: "start"
      set:
        stage: "confirmed"
    respond_with:
      status: 200
      body: { "ok": true, "step": 1 }
```

#### Cycle Responses

```yaml
rules:
  - name: "toggle-flag"
    url_contains: "/feature"
    cycle:
      - status: 200
        body: { "enabled": false }
      - status: 200
        body: { "enabled": true }
```

#### Random Responses

```yaml
rules:
  - name: "flaky"
    url_contains: "/health"
    random:
      - status: 200
        body: { "ok": true }
      - status: 500
        body: { "ok": false }
```

### Atomic Examples (Every Field)

Each snippet below is a standalone rule. You can paste into `rules.yaml` under `rules:`.

`quiet`:
```yaml
quiet: true
rules: []
```

`global_delay` fixed:
```yaml
global_delay: 0.3
rules: []
```

`global_delay` random:
```yaml
global_delay:
  random: [0.1, 0.6]
rules: []
```

`name`:
```yaml
- name: "basic"
  url_contains: "/api"
```

`enabled`:
```yaml
- name: "disabled"
  enabled: false
  url_contains: "/api"
```

`priority`:
```yaml
- name: "high-priority"
  priority: 100
  url_contains: "/api"
```

`probability`:
```yaml
- name: "flaky"
  probability: 0.3
  url_contains: "/api"
```

`count`:
```yaml
- name: "only-twice"
  count: 2
  url_contains: "/api"
```

`once`:
```yaml
- name: "only-once"
  once: true
  url_contains: "/api"
```

`quiet` (per-rule):
```yaml
- name: "quiet-rule"
  quiet: true
  url_contains: "/api"
```

`delay` fixed:
```yaml
- name: "delay-fixed"
  delay:
    fixed: 0.5
  url_contains: "/api"
```

`delay` random:
```yaml
- name: "delay-random"
  delay:
    random: [0.2, 1.0]
  url_contains: "/api"
```

`method`:
```yaml
- name: "post-only"
  method: "POST"
  url_contains: "/api"
```

`host`:
```yaml
- name: "host-regex"
  host: "^api\\.example\\.com$"
```

`url_contains`:
```yaml
- name: "url-contains"
  url_contains: "/v1/orders"
```

`url_regex`:
```yaml
- name: "url-regex"
  url_regex: "/v1/orders/\\d+"
```

`path_contains`:
```yaml
- name: "path-contains"
  path_contains: "/orders"
```

`path_regex`:
```yaml
- name: "path-regex"
  path_regex: "/orders/\\d+"
```

`status_code`:
```yaml
- name: "status-404"
  status_code: [404]
```

`header_contains`:
```yaml
- name: "ua-match"
  header_contains:
    user-agent: "Mozilla"
```

`query_contains`:
```yaml
- name: "query-match"
  query_contains:
    locale: "en"
```

`json_body`:
```yaml
- name: "json-body"
  json_body:
    $.user.id: 123
```

`json_body` exists:
```yaml
- name: "json-body-exists"
  json_body:
    $.user.id: { "$exists": true }
```

`response_json`:
```yaml
- name: "response-json"
  response_json:
    $.data.id: 123
```

`json_contains` (no JSONPath required — paste a trimmed example body):
```yaml
- name: "contains"
  json_contains:
    user: { role: "admin" }
    items:
      - { sku: "B" }
```

`json_contains` with a regex leaf:
```yaml
- name: "contains-regex"
  json_contains:
    email: { "$regex": "@example\\.com$" }
```

`response_json_contains`:
```yaml
- name: "resp-contains"
  response_json_contains:
    errors:
      - { code: "E1" }
```

`response_json` exists:
```yaml
- name: "response-json-exists"
  response_json:
    $.data.id: { "$exists": true }
```

`respond_with` inline JSON:
```yaml
- name: "respond-inline"
  respond_with:
    status: 200
    json: true
    body: { "ok": true }
```

`respond_with` file:
```yaml
- name: "respond-file"
  respond_with:
    status: 200
    file: "sample_response.json"
    headers:
      Content-Type: "application/json"
```

`respond_with` text:
```yaml
- name: "respond-text"
  respond_with:
    status: 200
    json: false
    body: "plain"
    headers:
      Content-Type: "text/plain"
```

`error`:
```yaml
- name: "inject-error"
  error:
    status: 500
    body: { "error": "boom" }
```

`error` file:
```yaml
- name: "error-file"
  error:
    status: 503
    file: "sample_response.json"
```

`modify_request_headers`:
```yaml
- name: "req-headers"
  modify_request_headers:
    set:
      X-Test: "1"
    remove: ["X-Remove"]
```

`modify_request_query`:
```yaml
- name: "req-query"
  modify_request_query:
    set:
      test: "1"
    remove: ["utm_source"]
```

`modify_request_json` set:
```yaml
- name: "req-json-set"
  modify_request_json:
    set:
      $.flag: true
```

`modify_request_json` append:
```yaml
- name: "req-json-append"
  modify_request_json:
    append:
      $.items:
        id: "NEW"
```

`modify_request_json` delete:
```yaml
- name: "req-json-delete"
  modify_request_json:
    delete:
      - $.items[?(@.id=="REMOVE")]
```

`modify_response_headers`:
```yaml
- name: "resp-headers"
  modify_response_headers:
    set:
      X-Proxy: "1"
    remove: ["X-Powered-By"]
```

`modify_response_json` set:
```yaml
- name: "resp-json-set"
  modify_response_json:
    set:
      $.flag: true
```

`modify_response_json` append:
```yaml
- name: "resp-json-append"
  modify_response_json:
    append:
      $.items:
        id: "EXTRA"
```

`modify_response_json` delete:
```yaml
- name: "resp-json-delete"
  modify_response_json:
    delete:
      - $.items[?(@.id=="REMOVE")]
```

`modify_response_json` merge (deep merge):
```yaml
- name: "resp-json-merge"
  modify_response_json:
    merge:
      $.user:
        role: "admin"
        prefs: { theme: "dark" }   # sibling keys under prefs survive
```

`modify_response_json` merge into array elements by predicate:
```yaml
- name: "resp-json-merge-where"
  modify_response_json:
    merge:
      $.items:
        $where: { sku: "A" }       # every element containing this shape
        $merge: { price: 0 }       # ...gets deep-merged; use $replace to swap it whole
```

`max_body_size`:
```yaml
- name: "limit"
  max_body_size: 1024
  modify_response_json:
    set:
      $.ok: true
```

## Contributing / Development

The [Install](#install) section covers the venv + editable install every contributor
also needs. A couple of extra details for hacking on Stunt itself:

Activating the venv on shells other than bash/zsh:

```bash
# fish
source venv/bin/activate.fish

# C shell (csh, tcsh)
source venv/bin/activate.csh

# Windows PowerShell
.\venv\Scripts\Activate.ps1
```

### Skip re-activating the venv every time

bash/zsh (`~/.bashrc` or `~/.zshrc`):
```bash
alias run_stunt='source /path/to/stunt/venv/bin/activate && stunt'
```

fish (`~/.config/fish/config.fish`):
```fish
function run_stunt
    source /path/to/stunt/venv/bin/activate.fish
    stunt
end
```

PowerShell (add to `$PROFILE`, then run `notepad $PROFILE` to edit it):
```powershell
function run_stunt {
  $venvPath = "C:\path\to\stunt\venv\Scripts\Activate.ps1"

  if (Test-Path $venvPath) {
    & $venvPath
    stunt
  } else {
    Write-Error "Virtual environment activation script not found at $venvPath"
  }
}
```

Then just run:
```bash
run_stunt
```

### Running the tests

```bash
pip install -e .[test]
pytest
```

## License

MIT
