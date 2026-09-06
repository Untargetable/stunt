# Contributing

## Setup

```bash
git clone https://github.com/Untargetable/stunt.git
cd stunt
python -m venv venv
source venv/bin/activate.fish     # or venv/bin/activate for bash/zsh
pip install -e ".[test]"
```

## Running tests

```bash
pytest                    # full suite (config in pyproject.toml)
pytest -k some_name       # a single test
pytest -m "not e2e"       # skip the mitmdump subprocess tests
```

Format with `black` and `isort` (`line-length = 120`) before committing.

## House style

- Shortest change that fixes the root cause. No speculative abstractions, no config
  knobs for values that never change, no interfaces with one implementation.
- Fix the bug where the callers actually converge, not in every caller that hits it.
- Every behaviour change needs a regression test that **fails before the fix and
  passes after it**. If you can't demonstrate the bug with a failing test first,
  you probably don't understand it yet.
- Update `README.md`, `rules.yaml`, and `docs/stunt_schema.json` together when
  you add or change a rules-file field — they're kept in sync by hand.
- Add an entry to `CHANGELOG.md` under `[Unreleased]` (create that heading if the
  top entry has already been tagged) for any user-visible change.

## Architecture

`src/stunt/addon.py` is the whole product. `cli.py` is a thin wrapper that shells out to
`mitmweb|mitmproxy|mitmdump -s addon.py` and owns `init` / `lint`. mitmproxy discovers the
addon via a module-level `load(loader)` plus `addons = [Stunt()]`.

### The phase model

`_process(flow, is_request)` runs from both the `request()` and `response()` hooks. Matching
is phase-agnostic; actions are phase-split:

| Phase | Actions |
|---|---|
| request | `error`, `kill`, `modify_request_*`, and `respond_with` / `cycle` / `random` |
| response | `modify_response_*`, and `respond_with` / `cycle` / `random` when `needs_response` |

`needs_response = bool(rule.status_codes or rule.response_json_matchers or rule.passthrough)`:
a rule that matches on the response cannot fire before one exists. Everything else
short-circuits in the request phase, which is why mocks work with no backend at all.

A rule pairing a response-only matcher with a request-phase action can never fire that
action. `_compile_rules` warns at load, and drops the rule when that is all it does.

**Side effects are committed only when the rule has an action for the current phase.** Hit
counters, `state.set`, `delay` and the scan-terminating `return` all sit behind that check.
Breaking it makes `once` serve nothing, `count: 3` serve one, state flows run backwards and
delays apply twice.

### Other invariants

- **Path resolution.** `_default_root()` at `load()` time: `$STUNT_HOME` → parent of
  `$VIRTUAL_ENV` → walk up from cwd → walk up from `addon.py`. `--rules` / `--mocks`
  override everything. `running()` creates `mocks/` only when the rules file exists, never
  speculatively.
- **The watcher** watches the rules file's *parent directory*, not the file path. Watching
  the path breaks permanently on the first rename-over save, which is how vim, VS Code and
  `sed -i` write. Rules reload on SHA-256 change; a touched mock clears the mtime-keyed cache.
- **Failure posture.** Matcher checks and action blocks use bare `except: continue` on
  purpose: a malformed rule or unparseable body must never break the proxy.
- Do not add an `error()` hook or a `_fix_content_length` helper. mitmproxy's `.content`
  setter already fixes `Content-Length` correctly, and re-fixing it corrupts compressed bodies.

### Adding a rules-file field

Four places, no exceptions:

1. Parse it in `_compile_rules()` and add it to `CompiledRule`.
2. Handle it in the matcher chain or the phase-appropriate action block of `_process()`.
3. Register the key in `RULE_KEYS` / `TOP_LEVEL_KEYS`, or it warns as an unknown key.
4. Add it to `docs/stunt_schema.json` (`additionalProperties: false`, so lint and the code
   will otherwise disagree).

Then check that `rules.yaml` still validates and `stunt lint` still returns `OK`.

Naming gotchas: inside `respond_with` / `error` the status key is `status`; the top-level
*matcher* is `status_code`. JSON bodies are only parsed under `max_body_size` (4 MB default).
Top-level `state:` sets initial values, while a rule-level `state:` given as a bare dict means
`require` — opposite meanings, same word.

## Tests

`tests/test_stunt.py` is the bulk, `tests/test_e2e.py` drives a real `mitmdump` process, and
`tests/test_packaging.py` guards the version single-sourcing.

**Drive tests through `request()` / `response()`, never `_process()` directly.** There are
zero direct `_process()` call sites and it must stay that way: bugs live in the seam between
the two hooks, and a test that bypasses them cannot see one. Keep timing deterministic by
patching `asyncio.sleep` and the RNG (`addon._rand`) rather than sleeping on the wall clock.

## Releasing (maintainer only)

Publishing is the maintainer's call, not something a contribution PR should do.
`pyproject.toml`'s `[project].version` is the single source of truth — nothing else
needs editing for the version number itself.

1. Bump the version in `pyproject.toml`.
2. Move `CHANGELOG.md`'s `[Unreleased]` section to a new `[<version>] - <date>` heading.
3. Commit, then tag and push:
   ```bash
   git tag -a v<version> -m "v<version>"
   git push origin v<version>
   ```
4. Build and verify:
   ```bash
   rm -rf dist build
   python -m pip install --upgrade build twine
   python -m build
   twine check dist/*
   ```
5. Upload to TestPyPI first and sanity-check the install:
   ```bash
   twine upload --repository testpypi dist/*
   pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ stunt
   ```
6. Once that looks right, upload for real:
   ```bash
   twine upload dist/*
   ```
