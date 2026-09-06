#!/usr/bin/env python3
"""
stunt — mitmproxy addon (see stunt.__version__ for the installed version)
"""

import asyncio
import copy
import difflib
import hashlib
import json
import logging
import math
import mimetypes
import os
import random
import re
import sys
from collections import Counter, OrderedDict

# mitmproxy's command system keys its type table on collections.abc generics;
# typing.Sequence[str] is a different object and is rejected.
from collections.abc import Sequence as AbcSequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import yaml
from jsonpath_ng import Child, Fields, Index
from jsonpath_ng.ext import parse as jp_parse
from mitmproxy import command, ctx, http
from watchfiles import awatch

logger = logging.getLogger("stunt")

# Every key the parser understands. Keep in sync with the raw.get()/raw[] calls
# in _reload (top-level) and _compile_rules (per-rule).
TOP_LEVEL_KEYS = frozenset({"quiet", "global_delay", "state", "rules", "mock", "defaults", "trace", "throttle"})
RULE_KEYS = frozenset(
    {
        "enabled",
        "name",
        "delay",
        "json_body",
        "response_json",
        "method",
        "json_contains",
        "response_json_contains",
        "host",
        "url_regex",
        "url_contains",
        "path_regex",
        "path_contains",
        "status_code",
        "header_contains",
        "query_contains",
        "max_body_size",
        "count",
        "once",
        "state",
        "cycle",
        "random",
        "respond_with",
        "error",
        "modify_request_headers",
        "modify_request_query",
        "modify_request_json",
        "modify_response_headers",
        "modify_response_json",
        "probability",
        "priority",
        "quiet",
        "passthrough",
        "kill",
        "throttle",
    }
)

# Recursion cap for _json_contains/_deep_merge. Well below the RecursionError
# threshold (~498 frames for _json_contains) and well above any real payload.
MAX_JSON_DEPTH = 100


# Recursive containment: is `pattern`'s shape present anywhere in `target`?
# Backs the json_contains matchers and `merge: { $where: ... }` element selection.
def _json_contains(pattern: Any, target: Any, _depth: int = 0) -> bool:
    if _depth > MAX_JSON_DEPTH:
        logger.warning(
            f"Stunt _json_contains: body nested deeper than MAX_JSON_DEPTH={MAX_JSON_DEPTH} "
            "— treating as no match rather than risking a RecursionError"
        )
        return False
    if isinstance(pattern, dict):
        if len(pattern) == 1 and "$regex" in pattern:
            if target is None:
                return False
            try:
                return bool(re.search(str(pattern["$regex"]), str(target)))
            except re.error:
                return False
        if not isinstance(target, dict):
            return False
        return all(k in target and _json_contains(v, target[k], _depth + 1) for k, v in pattern.items())
    if isinstance(pattern, list):
        if not isinstance(target, list):
            return False
        return all(any(_json_contains(p, t, _depth + 1) for t in target) for p in pattern)
    return pattern == target


# Recursive deep merge. Dicts merge key-wise; anything else overwrites.
def _deep_merge(target: Any, patch: Any, _depth: int = 0) -> Any:
    if _depth > MAX_JSON_DEPTH:
        logger.warning(
            f"Stunt _deep_merge: patch nested deeper than MAX_JSON_DEPTH={MAX_JSON_DEPTH} "
            "— leaving the existing value in place rather than risking a RecursionError"
        )
        return target
    if isinstance(patch, dict) and isinstance(target, dict):
        for k, v in patch.items():
            target[k] = _deep_merge(target.get(k), v, _depth + 1)
        return target
    return copy.deepcopy(patch)


def _is_project_root(path: Path) -> bool:
    return (
        (path / "rules.yaml").is_file()
        or (path / "mocks").is_dir()
        or (path / "pyproject.toml").is_file()
        or (path / ".git").is_dir()
    )


def _warn_unknown_keys(actual: Any, known: FrozenSet[str], where: str):
    if not isinstance(actual, dict):
        return
    for key in actual.keys() - known:
        suggestion = difflib.get_close_matches(str(key), known, n=1)
        hint = f" (did you mean '{suggestion[0]}'?)" if suggestion else ""
        logger.warning(f"Stunt {where}: unknown key '{key}'{hint}")


# `mock:` shorthand. Keys are "[METHOD ]path", values are either a bare
# body or a respond_with-shaped object.
HTTP_METHODS = frozenset(
    {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
        "TRACE",
        "CONNECT",
    }
)
RESPOND_WITH_KEYS = frozenset({"status", "body", "file", "headers", "json"})

MAX_DELAY = 10.0


def _clamp_delay(value: Any, where: str) -> float:
    """Clamp to MAX_DELAY, but say so instead of silently rewriting the user."""
    asked = float(value)
    applied = max(0.0, min(asked, MAX_DELAY))
    if applied != asked:
        logger.warning(f"Stunt {where}: delay {asked}s is outside the 0-{MAX_DELAY}s range " f"— using {applied}s")
    return applied


def _parse_delay(cfg: Any, where: str) -> Tuple[Optional[float], Optional[Tuple[float, float]]]:
    """A bare number, or a `{fixed, random}` mapping, into (fixed, range).

    `random` accepts a single number (a zero-width range) or a sequence, whose
    min/max become the range. `where` labels any clamp warning.
    """
    if isinstance(cfg, (int, float)) and not isinstance(cfg, bool):
        return _clamp_delay(cfg, where), None
    if not isinstance(cfg, dict):
        return None, None
    fixed = None
    if (raw_fixed := cfg.get("fixed")) is not None:
        fixed = _clamp_delay(raw_fixed, f"{where}.fixed")
    rng = None
    if "random" in cfg:
        rnd = cfg["random"]
        if isinstance(rnd, (int, float)):
            v = _clamp_delay(rnd, f"{where}.random")
            rng = (v, v)
        elif isinstance(rnd, Sequence) and rnd:
            clean = [_clamp_delay(x, f"{where}.random") for x in rnd]
            rng = (min(clean), max(clean))
    return fixed, rng


# Throttle presets: download rates in kbit/s, matching Chrome DevTools/Charles.
THROTTLE_PRESETS = {
    "gprs": 50,
    "2g": 240,
    "slow-3g": 400,
    "3g": 1600,
    "dsl": 2000,
    "4g": 4000,
    "wifi": 30000,
}

# Ceiling on a single throttled transfer, so `kbps: 0.001` warns instead of hanging.
MAX_THROTTLE_SECONDS = 30.0


def _parse_throttle(value: Any, where: str) -> Optional[float]:
    """Return a rate in BYTES per second, or None if the value is unusable."""
    if value is None:
        return None
    kbps: Any
    if isinstance(value, str):
        kbps = THROTTLE_PRESETS.get(value.strip().lower())
        if kbps is None:
            logger.warning(
                f"Stunt {where}: unknown throttle preset '{value}' — "
                f"known presets: {', '.join(sorted(THROTTLE_PRESETS))}"
            )
            return None
    elif isinstance(value, dict):
        kbps = value.get("kbps")
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        kbps = value
    else:
        kbps = None
    try:
        kbps = float(kbps)
    except (TypeError, ValueError):
        logger.warning(f"Stunt {where}: throttle needs a preset name or {{kbps: N}}")
        return None
    if kbps <= 0:
        logger.warning(f"Stunt {where}: throttle kbps must be > 0 — IGNORED")
        return None
    return kbps * 1000.0 / 8.0


def _desugar_mock(mock: Any) -> List[dict]:
    """Turn a `mock:` map into ordinary rule dicts. No parallel execution path."""
    if mock is None:
        return []
    if not isinstance(mock, dict):
        logger.warning("Stunt top-level 'mock' must be a map of '[METHOD ]path'")
        return []
    out: List[dict] = []
    for key, value in mock.items():
        head, _, tail = str(key).strip().partition(" ")
        if head.upper() in HTTP_METHODS and tail.strip():
            method, path = head.upper(), tail.strip()
        else:
            method, path = None, str(key).strip()
        if not path.startswith("/"):
            logger.warning(f"Stunt mock '{key}': path must start with '/' — IGNORED")
            continue
        # A dict is a respond_with object only if EVERY key is a respond_with key
        # and `status`, if present, is an int. Otherwise it is a bare body.
        if (
            isinstance(value, dict)
            and value
            and not (value.keys() - RESPOND_WITH_KEYS)
            and isinstance(value.get("status", 200), int)
            and not isinstance(value.get("status", 200), bool)
        ):
            respond_with = dict(value)
        else:
            respond_with = {"body": value}
        rule = {
            "name": f"mock {key}",
            # Exact path, query string tolerated.
            "path_regex": f"^{re.escape(path)}(\\?.*)?$",
            "respond_with": respond_with,
            # Below the default priority of 0, so an explicit rule always wins.
            "priority": -1,
        }
        if method:
            rule["method"] = method
        out.append(rule)
    return out


def _walk_up_for_root(start: Path) -> Optional[Path]:
    cur = start.resolve()
    for parent in (cur, *cur.parents):
        if _is_project_root(parent):
            return parent
    return None


def _default_root() -> Path:
    env_home = os.environ.get("STUNT_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()

    venv = os.environ.get("VIRTUAL_ENV")
    if venv:
        candidate = Path(venv).resolve().parent
        if _is_project_root(candidate):
            return candidate

    found = _walk_up_for_root(Path.cwd())
    if found:
        return found

    found = _walk_up_for_root(Path(__file__).resolve().parent)
    if found:
        return found

    return Path.cwd()


def _match_header_contains(pairs: FrozenSet[Tuple[str, str]], req: Any) -> Optional[str]:
    headers_lc: Dict[str, List[str]] = {}
    for hk, hv in req.headers.items():
        headers_lc.setdefault(hk.lower(), []).append(hv)
    if all(any(want in val for val in headers_lc.get(h, [])) for h, want in pairs):
        return None
    return "header_contains not satisfied"


# Request-side matcher chain, in evaluation order: (CompiledRule field, predicate,
# reason if the request is unreadable). The predicate runs only when the field is
# truthy and returns None to accept, or the rejection reason to reject.
_REQUEST_MATCHERS: Tuple[Tuple[str, Callable[[Any, Any], Optional[str]], str], ...] = (
    (
        "methods",
        lambda v, req: None if req.method.upper() in v else f"method {req.method!r} not in {sorted(v)}",
        "method could not be read from the request",
    ),
    (
        "host_regex",
        lambda v, req: None if req.host and v.search(req.host) else f"host_regex did not match host {req.host!r}",
        "host could not be read from the request",
    ),
    (
        "url_contains",
        lambda v, req: None if v in req.url else f"url_contains {v!r} not found in url",
        "url could not be read from the request",
    ),
    (
        "url_regex",
        lambda v, req: None if v.search(req.url) else "url_regex did not match url",
        "url could not be read from the request",
    ),
    (
        "path_contains",
        lambda v, req: None if v in req.path else f"path_contains {v!r} not found in path {req.path!r}",
        "path could not be read from the request",
    ),
    (
        "path_regex",
        lambda v, req: None if v.search(req.path) else f"path_regex did not match {req.path!r}",
        "path could not be read from the request",
    ),
    ("header_contains", _match_header_contains, "header_contains check failed"),
    (
        "query_contains",
        lambda v, req: (
            None
            if all(any(want in str(val) for val in req.query.get_all(k)) for k, want in v)
            else "query_contains not satisfied"
        ),
        "query_contains check failed",
    ),
)


@dataclass(frozen=True)
class CompiledRule:
    name: str
    probability: float = 1.0
    priority: int = 0
    count: Optional[int] = None
    once: bool = False
    quiet: Optional[bool] = None
    state_require: Tuple[Tuple[str, Any], ...] = ()
    state_set: Optional[dict] = None
    state_delete: FrozenSet[str] = frozenset()
    delay_fixed: Optional[float] = None
    delay_range: Optional[Tuple[float, float]] = None
    methods: FrozenSet[str] = frozenset()
    host_regex: Optional[re.Pattern] = None
    url_contains: Optional[str] = None
    url_regex: Optional[re.Pattern] = None
    path_contains: Optional[str] = None
    path_regex: Optional[re.Pattern] = None
    status_codes: FrozenSet[int] = frozenset()
    header_contains: FrozenSet[Tuple[str, str]] = frozenset()
    query_contains: FrozenSet[Tuple[str, str]] = frozenset()
    json_matchers: Tuple[Tuple[Any, Any], ...] = ()
    response_json_matchers: Tuple[Tuple[Any, Any], ...] = ()
    json_contains: Any = None
    response_json_contains: Any = None
    mod_req_headers: Optional[dict] = None
    mod_req_query: Optional[dict] = None
    mod_req_json: Optional[dict] = None
    mod_resp_headers: Optional[dict] = None
    mod_resp_json: Optional[dict] = None
    inject_error: Optional[dict] = None
    respond_with: Optional[dict] = None
    respond_with_cycle: Tuple[Dict[str, Any], ...] = ()
    respond_with_random: Tuple[Dict[str, Any], ...] = ()
    passthrough: bool = False
    kill: bool = False
    throttle_bps: Optional[float] = None
    max_body_size: int = 4 * 1024 * 1024

    # Phase properties of the rule, derived once instead of per flow.
    response_only: bool = field(init=False)  # cannot be evaluated before a response exists
    needs_response: bool = field(init=False)  # ...or is explicitly told to wait for one
    has_respond: bool = field(init=False)  # serves a mock (respond_with / cycle / random)
    req_actions: bool = field(init=False)  # acts in the request phase
    resp_actions: bool = field(init=False)  # acts in the response phase
    inert: bool = field(init=False)  # delay-only / state-only: no body action at all

    def __post_init__(self):
        response_only = bool(
            self.status_codes or self.response_json_matchers or self.response_json_contains is not None
        )
        has_respond = bool(self.respond_with or self.respond_with_cycle or self.respond_with_random)
        req_actions = bool(
            self.inject_error or self.kill or self.mod_req_headers or self.mod_req_query or self.mod_req_json
        )
        resp_actions = bool(self.mod_resp_headers or self.mod_resp_json)
        for k, v in (
            ("response_only", response_only),
            ("needs_response", response_only or self.passthrough),
            ("has_respond", has_respond),
            ("req_actions", req_actions),
            ("resp_actions", resp_actions),
            ("inert", not (has_respond or req_actions or resp_actions)),
        ):
            object.__setattr__(self, k, v)


class Stunt:
    def __init__(self):
        self.rules: Tuple[CompiledRule, ...] = ()
        self.global_delay_fixed: Optional[float] = None
        self.global_delay_range: Optional[Tuple[float, float]] = None
        self.global_throttle_bps: Optional[float] = None
        self.quiet: bool = False
        # Tracing is on if either --trace-matches (trace_cli) or the rules.yaml
        # `trace:` key (trace_yaml) asks for it.
        self.trace_cli: bool = False
        self.trace: bool = False
        self._hash = ""
        self._mock_cache: "OrderedDict[str, Tuple[int, bytes]]" = OrderedDict()
        self._rule_hits: Dict[str, int] = {}
        self._state: Dict[str, Any] = {}
        self._cycle_index: Dict[str, int] = {}
        self._lock = asyncio.Lock()
        self._rand = random.Random()
        self._watcher_task: Optional[asyncio.Task] = None
        self._watcher_stop: Optional[asyncio.Event] = None
        self.rules_file: Optional[Path] = None
        self.mocks_dir: Optional[Path] = None
        # None unless --record was passed.
        self._recorder: Optional["Recorder"] = None
        # Session-only rule toggles. Disabled rules are lifted out of self.rules so
        # the matcher chain needs no extra check; _rules_all keeps the full ordered
        # set so re-enabling restores the rule's position.
        self._disabled: Dict[str, bool] = {}
        self._rules_all: Tuple[CompiledRule, ...] = ()

    async def _reload(self):
        if self.rules_file is None or not self.rules_file.exists():
            async with self._lock:
                if self._hash:
                    self.rules = ()
                    self._rules_all = ()
                    self._disabled.clear()
                    self.global_delay_fixed = None
                    self.global_delay_range = None
                    self.global_throttle_bps = None
                    self.quiet = False
                    self.trace = self.trace_cli
                    self._hash = ""
                    self._rule_hits.clear()
                    self._state.clear()
                    self._cycle_index.clear()
                    self._mock_cache.clear()
                    logger.info("Stunt Rules file deleted — addon reset")
            return
        try:
            content = self.rules_file.read_bytes()
            new_hash = hashlib.sha256(content).hexdigest()
            if new_hash == self._hash:
                return
            data = yaml.safe_load(content) or {}
            _warn_unknown_keys(data, TOP_LEVEL_KEYS, "top-level")
            compiled = self._compile_rules(data)
            compiled_sorted = tuple(sorted(compiled, key=lambda r: r.priority, reverse=True))
            global_delay_fixed = None
            global_delay_range = None
            if "global_delay" in data:
                gd = data.get("global_delay")
                if isinstance(gd, (int, float)):
                    global_delay_fixed, _ = _parse_delay(gd, "global_delay")
                elif isinstance(gd, dict) and "random" in gd:
                    # A bad global_delay.random warns; a bad scalar above is fatal.
                    try:
                        global_delay_fixed, global_delay_range = _parse_delay(gd, "global_delay")
                    except Exception as e:
                        logger.warning(f"Stunt: bad global_delay.random: {e}")
            global_throttle_bps = _parse_throttle(data.get("throttle"), "top-level")
            quiet = bool(data.get("quiet", False))
            trace = self.trace_cli or bool(data.get("trace", False))
            initial_state = data.get("state")
            if not isinstance(initial_state, dict):
                initial_state = {}
            async with self._lock:
                self.rules = compiled_sorted
                # Session toggles belong to the rule set they were made against.
                self._disabled.clear()
                self._rules_all = compiled_sorted
                self.global_delay_fixed = global_delay_fixed
                self.global_delay_range = global_delay_range
                self.global_throttle_bps = global_throttle_bps
                self.quiet = quiet
                self.trace = trace
                self._hash = new_hash
                self._mock_cache.clear()
                self._rule_hits.clear()
                self._state = dict(initial_state)
                self._cycle_index.clear()
            logger.info(f"Stunt Reloaded {len(self.rules)} rules")
        except Exception as e:
            logger.error(f"Stunt Failed to load rules → {e}")

    @staticmethod
    def _compile_rules(config: dict) -> List[CompiledRule]:
        rules = []
        # Shallow-merge `defaults:` under every rule; the rule's own key wins.
        defaults = config.get("defaults") or {}
        if not isinstance(defaults, dict):
            logger.warning("Stunt top-level 'defaults' must be an object")
            defaults = {}
        _warn_unknown_keys(defaults, RULE_KEYS, "defaults")
        raw_rules = list(config.get("rules") or []) + _desugar_mock(config.get("mock"))
        for idx, raw in enumerate(raw_rules):
            if defaults:
                raw = {**defaults, **raw}
            if not raw.get("enabled", True):
                continue
            name = raw.get("name", f"unnamed_rule_{idx+1}")
            _warn_unknown_keys(raw, RULE_KEYS, f"Rule '{name}'")
            try:

                def _compile_variants(raw_variants: Any, kind: str) -> Tuple[Dict[str, Any], ...]:
                    if raw_variants is None:
                        return ()
                    if isinstance(raw_variants, (str, bytes, dict)) or not isinstance(raw_variants, Sequence):
                        logger.warning(f"Stunt Rule '{name}': {kind} must be a list of objects")
                        return ()
                    compiled: List[Dict[str, Any]] = []
                    for v_idx, item in enumerate(raw_variants):
                        if not isinstance(item, dict):
                            logger.warning(f"Stunt Rule '{name}': {kind}[{v_idx}] must be an object")
                            continue
                        compiled.append(item)
                    return tuple(compiled)

                delay_fixed, delay_range = _parse_delay(raw.get("delay"), f"Rule '{name}' delay")
                throttle_bps = _parse_throttle(raw.get("throttle"), f"Rule '{name}'")
                kill = bool(raw.get("kill", False))

                def _compile_json_matchers(key: str) -> List[Tuple[Any, Any]]:
                    out: List[Tuple[Any, Any]] = []
                    for path, value in (raw.get(key) or {}).items():
                        try:
                            expr = jp_parse(path)
                            if isinstance(value, dict) and "$exists" in value:
                                out.append((expr, ("exists", bool(value["$exists"]))))
                            else:
                                out.append((expr, value))
                        except Exception as e:
                            logger.warning(f"Stunt Rule '{name}': invalid {key} '{path}': {e}")
                    return out

                json_matchers = _compile_json_matchers("json_body")
                response_json_matchers = _compile_json_matchers("response_json")
                json_contains = raw.get("json_contains")
                response_json_contains = raw.get("response_json_contains")
                for key, val in (("json_contains", json_contains), ("response_json_contains", response_json_contains)):
                    if val is not None and not isinstance(val, (dict, list)):
                        logger.warning(f"Stunt Rule '{name}': {key} must be an object or a list — ignored")
                        if key == "json_contains":
                            json_contains = None
                        else:
                            response_json_contains = None
                raw_methods = raw.get("method")
                if raw_methods is None:
                    methods = frozenset()
                else:
                    if isinstance(raw_methods, str):
                        raw_methods = [raw_methods]
                    methods_list = [m.upper() for m in raw_methods if str(m).strip() and m != "*"]
                    methods = frozenset(methods_list)
                    if any(str(m).strip() == "*" for m in raw_methods):
                        methods = frozenset()

                def _compile_matcher_re(key: str, label: str) -> Optional[re.Pattern]:
                    if not raw.get(key):
                        return None
                    try:
                        return re.compile(raw[key])
                    except Exception as e:
                        logger.warning(f"Stunt Rule '{name}': invalid {label}: {e}")
                        return None

                host_re = _compile_matcher_re("host", "host regex")
                url_re = _compile_matcher_re("url_regex", "url_regex")
                path_re = _compile_matcher_re("path_regex", "path_regex")
                status_raw = raw.get("status_code")
                status_codes: FrozenSet[int] = frozenset()
                if status_raw is not None:
                    try:
                        if isinstance(status_raw, (int, str)):
                            status_codes = frozenset([int(status_raw)])
                        elif isinstance(status_raw, Sequence):
                            status_codes = frozenset(int(x) for x in status_raw)
                        else:
                            raise TypeError("status_code must be int, str, or sequence")
                    except Exception as e:
                        logger.warning(f"Stunt Rule '{name}': invalid status_code: {e}")
                        status_codes = frozenset()
                header_contains = frozenset((k.lower(), str(v)) for k, v in (raw.get("header_contains") or {}).items())
                query_contains = frozenset((k, str(v)) for k, v in (raw.get("query_contains") or {}).items())
                try:
                    max_body_size = int(raw.get("max_body_size", 4 * 1024 * 1024))
                    if max_body_size < 0:
                        raise ValueError("max_body_size must be >= 0")
                except Exception as e:
                    logger.warning(f"Stunt Rule '{name}': invalid max_body_size: {e}")
                    max_body_size = 4 * 1024 * 1024
                raw_count = raw.get("count")
                count_val: Optional[int]
                if raw_count is None:
                    count_val = None
                else:
                    try:
                        count_val = int(raw_count)
                        if count_val < 0:
                            raise ValueError("count must be >= 0")
                    except Exception as e:
                        logger.warning(f"Stunt Rule '{name}': invalid count: {e}")
                        count_val = None
                state_require: Tuple[Tuple[str, Any], ...] = ()
                state_set: Optional[dict] = None
                state_delete: FrozenSet[str] = frozenset()
                state_raw = raw.get("state")
                if state_raw is not None:
                    if isinstance(state_raw, dict):
                        if any(k in state_raw for k in ("require", "set", "delete")):
                            require_raw = state_raw.get("require") or {}
                            set_raw = state_raw.get("set") or {}
                            delete_raw = state_raw.get("delete") or []
                        else:
                            require_raw = state_raw
                            set_raw = {}
                            delete_raw = []
                        if not isinstance(require_raw, dict):
                            logger.warning(f"Stunt Rule '{name}': state.require must be an object")
                        else:
                            req_items: List[Tuple[str, Any]] = []
                            for key, value in require_raw.items():
                                if isinstance(value, dict) and "$exists" in value:
                                    req_items.append((str(key), ("exists", bool(value["$exists"]))))
                                else:
                                    req_items.append((str(key), value))
                            state_require = tuple(req_items)
                        if set_raw:
                            if isinstance(set_raw, dict):
                                state_set = dict(set_raw)
                            else:
                                logger.warning(f"Stunt Rule '{name}': state.set must be an object")
                                state_set = None
                        if delete_raw:
                            if isinstance(delete_raw, Sequence) and not isinstance(delete_raw, (str, bytes, dict)):
                                state_delete = frozenset(str(k) for k in delete_raw)
                            else:
                                logger.warning(f"Stunt Rule '{name}': state.delete must be a list")
                    else:
                        logger.warning(f"Stunt Rule '{name}': state must be an object")
                cycle_raw = raw.get("cycle")
                random_raw = raw.get("random")
                if cycle_raw is not None and random_raw is not None:
                    logger.warning(f"Stunt Rule '{name}': both cycle and random provided; cycle takes precedence")
                respond_with_cycle = _compile_variants(cycle_raw, "cycle")
                respond_with_random = _compile_variants(random_raw, "random") if not respond_with_cycle else ()
                # A typo'd action key (`respond:` for `respond_with:`) would match and
                # then silently swallow the rest of the scan. Reject it at load.
                if (
                    not (
                        respond_with_cycle
                        or respond_with_random
                        or any(
                            raw.get(k)
                            for k in (
                                "respond_with",
                                "error",
                                "modify_request_headers",
                                "modify_request_query",
                                "modify_request_json",
                                "modify_response_headers",
                                "modify_response_json",
                            )
                        )
                    )
                    and delay_fixed is None
                    and not delay_range
                    and not state_set
                    and not state_delete
                    and not kill
                    and throttle_bps is None
                ):
                    logger.warning(
                        f"Stunt Rule '{name}': no recognised action " f"(typo in an action key?) — rule is IGNORED"
                    )
                    continue
                rule = CompiledRule(
                    name=name,
                    probability=max(0.0, min(1.0, float(raw.get("probability", 1.0)))),
                    priority=int(raw.get("priority", 0)),
                    count=count_val,
                    once=bool(raw.get("once", False)),
                    quiet=raw.get("quiet"),
                    state_require=state_require,
                    state_set=state_set,
                    state_delete=state_delete,
                    delay_fixed=delay_fixed,
                    delay_range=delay_range,
                    methods=methods,
                    host_regex=host_re,
                    url_contains=raw.get("url_contains"),
                    url_regex=url_re,
                    path_contains=raw.get("path_contains"),
                    path_regex=path_re,
                    status_codes=status_codes,
                    header_contains=header_contains,
                    query_contains=query_contains,
                    json_matchers=tuple(json_matchers),
                    response_json_matchers=tuple(response_json_matchers),
                    json_contains=json_contains,
                    response_json_contains=response_json_contains,
                    mod_req_headers=raw.get("modify_request_headers"),
                    mod_req_query=raw.get("modify_request_query"),
                    mod_req_json=raw.get("modify_request_json"),
                    mod_resp_headers=raw.get("modify_response_headers"),
                    mod_resp_json=raw.get("modify_response_json"),
                    inject_error=raw.get("error"),
                    respond_with=raw.get("respond_with"),
                    respond_with_cycle=respond_with_cycle,
                    respond_with_random=respond_with_random,
                    passthrough=bool(raw.get("passthrough", False)),
                    kill=kill,
                    throttle_bps=throttle_bps,
                    max_body_size=max_body_size,
                )
                # A response-only matcher (status_code / response_json*) keeps the rule
                # out of the request phase, where error/kill/modify_request_* are the only
                # actions that run. Such a pairing can never fire.
                if rule.response_only and rule.req_actions:
                    dead = ", ".join(
                        k
                        for k in (
                            "error",
                            "kill",
                            "modify_request_headers",
                            "modify_request_query",
                            "modify_request_json",
                        )
                        if raw.get(k)
                    )
                    fatal = not (rule.resp_actions or rule.has_respond)
                    logger.warning(
                        f"Stunt Rule '{name}': {dead} run in the request phase, but "
                        f"status_code/response_json only match once a response exists — "
                        f"{'rule is IGNORED' if fatal else 'that action can never fire'}"
                    )
                    if fatal:
                        continue
                rules.append(rule)
            except Exception as e:
                logger.error(f"Stunt Rule '{name}' failed to compile and is IGNORED: {e}")
        return rules

    async def _delay(self, fixed: Optional[float], rng: Optional[Tuple[float, float]]):
        if fixed is not None:
            await asyncio.sleep(fixed)
        elif rng:
            await asyncio.sleep(self._rand.uniform(*rng))

    async def _throttle(self, flow: http.HTTPFlow):
        """Model transfer time as body_size / rate.

        Called once per flow, on whichever phase first has a response body. The
        metadata flag keeps it from applying twice.

        Approximate: the whole body is handed over after a single sleep, not paced
        packet by packet.
        """
        if flow.response is None or flow.metadata.get("stunt_throttled"):
            return
        rate = flow.metadata.get("stunt_throttle") or self.global_throttle_bps
        if not rate:
            return
        flow.metadata["stunt_throttled"] = True
        seconds = len(flow.response.content or b"") / rate
        if seconds > MAX_THROTTLE_SECONDS:
            logger.warning(
                f"Stunt throttle: transfer of {len(flow.response.content or b'')} bytes "
                f"would take {seconds:.1f}s — capped at {MAX_THROTTLE_SECONDS}s"
            )
            seconds = MAX_THROTTLE_SECONDS
        if seconds > 0:
            await asyncio.sleep(seconds)

    @staticmethod
    def _safe_json(content: bytes, limit: int) -> Optional[Any]:
        if not content or len(content) > limit:
            return None
        try:
            return json.loads(content.decode("utf-8", errors="replace"))
        except Exception:
            return None

    @staticmethod
    def _safe_repr(value: Any, max_len: int = 200) -> str:
        try:
            r = repr(value)
            if len(r) > max_len:
                r = r[:max_len] + "..."
            return r
        except Exception:
            return "<unrepresentable value>"

    def _matcher_match(self, expr: Any, exp: Any, body: Any) -> bool:
        if isinstance(exp, tuple) and exp[0] == "exists":
            has = bool(expr.find(body))
            if exp[1]:
                return has
            return not has
        return any(m.value == exp for m in expr.find(body))

    def _apply_json_mods(self, data: Any, mods: Dict[str, Any], log_func: Optional[Callable[[str], None]] = None):
        if not mods:
            return

        # --- DELETE ---
        for path_str in mods.get("delete") or []:
            try:
                expr = jp_parse(path_str)
                matches = list(expr.find(data))

                if not matches:
                    continue

                # Group deletions by parent object to avoid index shift issues
                parents = {}

                for match in matches:
                    if match.context is None:
                        continue

                    parent = match.context.value
                    step = match.path.right if isinstance(match.path, Child) else match.path

                    parents.setdefault(id(parent), []).append((parent, step))

                deleted_count = 0

                for parent_entries in parents.values():
                    parent = parent_entries[0][0]

                    # Handle list deletions safely (reverse sorted indexes)
                    if isinstance(parent, list):
                        indexes = []
                        for _, step in parent_entries:
                            if isinstance(step, Index):
                                for idx in step.indices:
                                    if isinstance(idx, int):
                                        indexes.append(idx)
                        indexes = sorted(set(indexes), reverse=True)
                        for idx in indexes:
                            if 0 <= idx < len(parent):
                                del parent[idx]
                                deleted_count += 1

                    elif isinstance(parent, dict):
                        for _, step in parent_entries:
                            if isinstance(step, Fields):
                                for field in step.fields:
                                    if field in parent:
                                        parent.pop(field, None)
                                        deleted_count += 1

                if deleted_count > 0 and log_func:
                    log_func(f"Deleted {deleted_count} match(es) for JSON path: {path_str}")

            except Exception as e:
                if log_func:
                    log_func(f"Delete failed for {path_str}: {e}")
                continue

        # --- SET ---
        for path_str, value in (mods.get("set") or {}).items():
            try:
                expr = jp_parse(path_str)
                expr.update_or_create(data, value)
                if log_func:
                    log_func(f"Set JSON path {path_str} to: {self._safe_repr(value)}")
            except Exception:
                pass

        # --- APPEND ---
        for path_str, value in (mods.get("append") or {}).items():
            try:
                expr = jp_parse(path_str)
                appended_count = 0
                for match in expr.find(data):
                    if isinstance(match.value, list):
                        if isinstance(value, list):
                            match.value.extend(value)
                        else:
                            match.value.append(value)
                        appended_count += 1
                if appended_count > 0 and log_func:
                    log_func(
                        f"Appended to {appended_count} match(es) for JSON path {path_str}: {self._safe_repr(value)}"
                    )
            except Exception:
                continue

        # --- MERGE ---
        # Two shapes per JSONPath key:
        #   "$.a": {...}                          -> deep-merge the object into each match
        #   "$.a": {$where: {...}, $merge: {...}} -> match is a list; deep-merge into every
        #                                            element containing the $where shape
        #   "$.a": {$where: {...}, $replace: X}   -> ...replace the whole element with X
        # The "$"-prefixed keys mirror the existing $exists/$regex sentinels, and keep a
        # literal payload key named "where"/"merge" from being mistaken for a directive.
        merge_spec = mods.get("merge")
        for path_str, spec in (merge_spec if isinstance(merge_spec, dict) else {}).items():
            try:
                expr = jp_parse(path_str)
                predicate = spec.get("$where") if isinstance(spec, dict) else None
                merged_count = 0
                for match in expr.find(data):
                    target = match.value
                    if predicate is None:
                        if isinstance(target, dict) and isinstance(spec, dict):
                            _deep_merge(target, spec)
                            merged_count += 1
                        continue
                    if not isinstance(target, list):
                        continue
                    for i, element in enumerate(target):
                        if not _json_contains(predicate, element):
                            continue
                        if "$replace" in spec:
                            target[i] = copy.deepcopy(spec["$replace"])
                        elif "$merge" in spec:
                            target[i] = _deep_merge(element, spec["$merge"])
                        else:
                            continue
                        merged_count += 1
                if merged_count > 0 and log_func:
                    log_func(f"Merged into {merged_count} match(es) for JSON path {path_str}")
            except Exception as e:
                if log_func:
                    log_func(f"Merge failed for {path_str}: {e}")
                continue

    @staticmethod
    def _state_holds(state: Dict[str, Any], rule: CompiledRule) -> bool:
        """Pure predicate. Callers decide what lock, if any, it is evaluated under."""
        for key, expected in rule.state_require:
            if isinstance(expected, tuple) and expected[0] == "exists":
                has = key in state
                if expected[1] and not has:
                    return False
                if not expected[1] and has:
                    return False
            else:
                if key not in state or state[key] != expected:
                    return False
        return True

    async def _state_match(self, rule: CompiledRule) -> bool:
        """Cheap pre-filter so a rule whose gate is shut skips the matcher chain.

        Advisory only; the authoritative check runs under the commit lock in
        _try_consume_rule.
        """
        if not rule.state_require:
            return True
        async with self._lock:
            return self._state_holds(self._state, rule)

    async def _next_cycle_respond_with(self, rule: CompiledRule) -> Dict[str, Any]:
        async with self._lock:
            idx = self._cycle_index.get(rule.name, 0)
            variant = rule.respond_with_cycle[idx % len(rule.respond_with_cycle)]
            self._cycle_index[rule.name] = (idx + 1) % len(rule.respond_with_cycle)
        return variant

    async def _load_mock(self, filename: str) -> bytes:
        path = (self.mocks_dir / filename).resolve()
        base = self.mocks_dir.resolve()
        if not path.is_relative_to(base):
            raise PermissionError(f"Path traversal attempt blocked: {filename}")
        if not path.exists() or not path.is_file():
            raise FileNotFoundError(f"Mock file not found: {filename}")
        mtime_ns = path.stat().st_mtime_ns
        async with self._lock:
            cached = self._mock_cache.get(filename)
            if cached and cached[0] == mtime_ns:
                self._mock_cache.move_to_end(filename)
                return cached[1]
        content = path.read_bytes()
        async with self._lock:
            self._mock_cache[filename] = (mtime_ns, content)
            if len(self._mock_cache) > 100:
                self._mock_cache.popitem(last=False)
        return content

    async def _try_consume_rule(self, rule: CompiledRule) -> bool:
        """Commit the rule: gate, budget and state mutation in ONE critical section.

        Nothing may await between the check and the write, or concurrent flows all
        observe an open gate before any of them shuts it.
        """
        key = rule.name
        async with self._lock:
            if rule.state_require and not self._state_holds(self._state, rule):
                return False
            hits = self._rule_hits.get(key, 0)
            if rule.once and hits >= 1:
                return False
            if rule.count is not None and hits >= rule.count:
                return False
            self._rule_hits[key] = hits + 1
            for k in rule.state_delete:
                self._state.pop(k, None)
            if rule.state_set:
                for k, v in rule.state_set.items():
                    self._state[k] = v
            return True

    def _log(self, rule: CompiledRule, message: str):
        if rule.quiet:
            return
        if rule.quiet is False or (rule.quiet is None and not self.quiet):
            logger.warning(f"Stunt [{rule.name}] {message}")

    @staticmethod
    def _emit_trace(flow: http.HTTPFlow, is_request: bool, lines: List[str]):
        """One readable block per request, not a line per rule per log call."""
        phase = "request" if is_request else "response"
        req = flow.request
        header = f"Stunt trace [{phase}] {req.method} {req.path}"
        logger.info(header + "\n  " + "\n  ".join(lines))

    async def _process(self, flow: http.HTTPFlow, is_request: bool):
        if not is_request and flow.metadata.get("stunt_handled", False):
            return
        req = flow.request
        if is_request:
            await self._delay(self.global_delay_fixed, self.global_delay_range)
        rules = self.rules
        # None when tracing is off, so the hot path costs one attribute check.
        trace_lines: Optional[List[str]] = [] if (self.trace and not self.quiet) else None

        def trace(rule: CompiledRule, reason: str):
            if trace_lines is not None:
                trace_lines.append(f"rule '{rule.name}': {reason}")

        for rule in rules:
            # A rule matching on the response cannot be decided before one exists.
            if is_request and rule.response_only:
                trace(
                    rule, "matches on the response phase only (status_code/response_json*) — not evaluated on request"
                )
                continue
            hits = self._rule_hits.get(rule.name, 0)
            if rule.once and hits >= 1:
                trace(rule, "already fired (once: true)")
                continue
            if rule.count is not None and hits >= rule.count:
                trace(rule, f"count limit reached ({rule.count})")
                continue
            if rule.state_require:
                if not await self._state_match(rule):
                    trace(rule, "state.require not satisfied")
                    continue
            rejection = None
            for attr, check, unreadable in _REQUEST_MATCHERS:
                value = getattr(rule, attr)
                if not value:
                    continue
                try:
                    rejection = check(value, req)
                except Exception:
                    rejection = unreadable
                if rejection:
                    break
            if rejection:
                trace(rule, rejection)
                continue
            if rule.json_matchers or rule.json_contains is not None:
                req_content = req.content
                if req_content is None:
                    trace(rule, "json_body/json_contains matcher set but request has no body")
                    continue
                body = self._safe_json(req_content, rule.max_body_size)
                if body is None:
                    trace(rule, "json_body/json_contains matcher set but request body is not valid/small enough JSON")
                    continue
                try:
                    if not all(self._matcher_match(expr, exp, body) for expr, exp in rule.json_matchers):
                        trace(rule, "json_body did not match")
                        continue
                except Exception:
                    trace(rule, "json_body check failed")
                    continue
                try:
                    if rule.json_contains is not None and not _json_contains(rule.json_contains, body):
                        trace(rule, "json_contains did not match")
                        continue
                except Exception:
                    trace(rule, "json_contains check failed")
                    continue
            if rule.status_codes and flow.response and flow.response.status_code not in rule.status_codes:
                trace(rule, f"status_code {flow.response.status_code} not in {sorted(rule.status_codes)}")
                continue
            if (rule.response_json_matchers or rule.response_json_contains is not None) and flow.response:
                resp_content = flow.response.content
                if resp_content is None:
                    trace(rule, "response_json/response_json_contains matcher set but response has no body")
                    continue
                body = self._safe_json(resp_content, rule.max_body_size)
                if body is None:
                    trace(
                        rule,
                        "response_json/response_json_contains matcher set but response body is not valid/small enough JSON",
                    )
                    continue
                try:
                    if not all(self._matcher_match(expr, exp, body) for expr, exp in rule.response_json_matchers):
                        trace(rule, "response_json did not match")
                        continue
                except Exception:
                    trace(rule, "response_json check failed")
                    continue
                try:
                    if rule.response_json_contains is not None and not _json_contains(
                        rule.response_json_contains, body
                    ):
                        trace(rule, "response_json_contains did not match")
                        continue
                except Exception:
                    trace(rule, "response_json_contains check failed")
                    continue
            # Matching is phase-agnostic, actions are phase-split. Decide what THIS
            # phase can do before spending the delay, a hit, the state or the terminating
            # return. Mocks are served in the request phase, so they work with no upstream
            # at all; only a rule needing the real response (status_code /
            # response_json matchers, or passthrough) waits.
            respond_here = rule.has_respond and rule.needs_response != is_request
            if is_request:
                # A delay-only / state-only rule has no body action in either phase — run it here.
                has_action = respond_here or rule.req_actions or rule.inert
            else:
                has_action = respond_here or rule.resp_actions
            if not has_action:
                trace(rule, "matches, but its action belongs to the other phase")
                continue
            if rule.probability < 1.0:
                if self._rand.random() > rule.probability:
                    trace(rule, f"probability roll missed (p={rule.probability})")
                    continue
            await self._delay(rule.delay_fixed, rule.delay_range)
            if not await self._try_consume_rule(rule):
                trace(rule, "hit budget exhausted between matching and firing")
                continue
            # Recorded here, spent by _throttle() once a response body exists.
            if rule.throttle_bps:
                flow.metadata["stunt_throttle"] = rule.throttle_bps
            try:
                # Connection-level failure, which no synthetic status code can express.
                # Request phase only, so the upstream is never contacted.
                if is_request and rule.kill:
                    if flow.killable:
                        flow.kill()
                    self._log(rule, "Killed the connection")
                    flow.metadata["stunt_handled"] = True
                    if trace_lines is not None:
                        trace_lines.append(f"rule '{rule.name}': MATCHED -> killed the connection")
                        self._emit_trace(flow, is_request, trace_lines)
                    return
                if is_request and rule.inject_error:
                    file_val = rule.inject_error.get("file")
                    if file_val and isinstance(file_val, str):
                        body = await self._load_mock(file_val)
                    else:
                        body_val = rule.inject_error.get("body", "Error")
                        if isinstance(body_val, (dict, list)):
                            body = json.dumps(body_val, ensure_ascii=False).encode("utf-8")
                        else:
                            body = str(body_val).encode("utf-8")
                    flow.response = http.Response.make(
                        rule.inject_error.get("status", 500),
                        body,
                        rule.inject_error.get("headers", {"Content-Type": "application/json"}),
                    )
                    self._log(rule, "Injected error response")
                    flow.metadata["stunt_handled"] = True
                    if trace_lines is not None:
                        trace_lines.append(f"rule '{rule.name}': MATCHED -> injected error response")
                        self._emit_trace(flow, is_request, trace_lines)
                    return
                if respond_here:
                    respond_with = None
                    if rule.respond_with_cycle:
                        respond_with = await self._next_cycle_respond_with(rule)
                    elif rule.respond_with_random:
                        respond_with = self._rand.choice(rule.respond_with_random)
                    elif rule.respond_with:
                        respond_with = rule.respond_with
                    if respond_with:
                        r = respond_with
                        file_val = r.get("file")
                        if file_val and isinstance(file_val, str):
                            body = await self._load_mock(file_val)
                        elif r.get("json", True):
                            body = json.dumps(r.get("body", {}), ensure_ascii=False).encode("utf-8")
                        else:
                            body = str(r.get("body", "")).encode("utf-8")
                        headers = dict(r.get("headers", {}))
                        # An explicit Content-Type wins: a recorded `file:` mock carries
                        # the upstream's real type.
                        if not any(k.lower() == "content-type" for k in headers):
                            headers["Content-Type"] = "application/json" if r.get("json", True) else "text/plain"
                        flow.response = http.Response.make(
                            r.get("status", 200),
                            body,
                            headers,
                        )
                        self._log(rule, "Served mock response")
                        flow.metadata["stunt_handled"] = True
                        if trace_lines is not None:
                            trace_lines.append(f"rule '{rule.name}': MATCHED -> served mock response")
                            self._emit_trace(flow, is_request, trace_lines)
                        return
                if is_request:
                    if rule.mod_req_headers:
                        mod = rule.mod_req_headers
                        for k in mod.get("remove", []):
                            flow.request.headers.pop(k, None)
                            self._log(rule, f"Removed request header: {k}")
                        for k, v in mod.get("set", {}).items():
                            flow.request.headers[k] = str(v)
                            self._log(rule, f"Set request header: {k} = {v}")
                    if rule.mod_req_query:
                        mod = rule.mod_req_query
                        for k in mod.get("remove", []):
                            flow.request.query.pop(k, None)
                            self._log(rule, f"Removed request query param: {k}")
                        for k, v in mod.get("set", {}).items():
                            flow.request.query[k] = str(v)
                            self._log(rule, f"Set request query param: {k} = {v}")
                    if rule.mod_req_json:
                        # A non-JSON or oversized body is a no-op, not a reason to hand
                        # the flow on: the hit, state write and delay are already spent.
                        req_content = req.content
                        data = self._safe_json(req_content, rule.max_body_size) if req_content else None
                        if data is None:
                            self._log(rule, "modify_request_json skipped: body is not JSON within max_body_size")
                        else:
                            log_func = lambda msg: self._log(rule, msg)
                            self._apply_json_mods(data, rule.mod_req_json, log_func=log_func)
                            req.content = json.dumps(data, ensure_ascii=False).encode("utf-8")
                else:
                    if rule.mod_resp_headers and flow.response:
                        mod = rule.mod_resp_headers
                        for k in mod.get("remove", []):
                            flow.response.headers.pop(k, None)
                            self._log(rule, f"Removed response header: {k}")
                        for k, v in mod.get("set", {}).items():
                            flow.response.headers[k] = str(v)
                            self._log(rule, f"Set response header: {k} = {v}")
                    if rule.mod_resp_json and flow.response:
                        ct = (flow.response.headers.get("content-type") or "").lower()
                        if "json" in ct or not ct:
                            resp_content = flow.response.content
                            data = self._safe_json(resp_content, rule.max_body_size) if resp_content else None
                            if data is None:
                                self._log(rule, "modify_response_json skipped: body is not JSON within max_body_size")
                            else:
                                log_func = lambda msg: self._log(rule, msg)
                                self._apply_json_mods(data, rule.mod_resp_json, log_func=log_func)
                                flow.response.content = json.dumps(data, ensure_ascii=False).encode("utf-8")
            except OSError as e:
                logger.warning(f"Stunt Rule '{rule.name}': mock file unavailable → {e}")
                trace(rule, f"action raised: mock file unavailable → {e}")
                continue
            except Exception as e:
                logger.error(f"Stunt: error applying action for '{rule.name}': {e}")
                trace(rule, f"action raised: {e}")
                continue
            if trace_lines is not None:
                trace_lines.append(f"rule '{rule.name}': MATCHED -> applied")
                self._emit_trace(flow, is_request, trace_lines)
            return
        if trace_lines is not None:
            trace_lines.append(
                "no rule matched — falling through to the real backend" if is_request else "no rule matched"
            )
            self._emit_trace(flow, is_request, trace_lines)

    async def _watcher(self):
        await self._reload()
        if self.rules_file is None or self.mocks_dir is None:
            return
        # Watch the parent DIRECTORY, not the file: a rename-over save (vim, VS Code,
        # `sed -i`) replaces the inode and orphans a watch on the path itself.
        rules_dir = self.rules_file.resolve().parent
        mocks_dir = self.mocks_dir.resolve()
        watch_paths = (rules_dir,) if mocks_dir.is_relative_to(rules_dir) else (rules_dir, mocks_dir)
        try:
            # rules.yaml usually sits at a project root; without this filter every
            # write under venv/, .git/ and node_modules/ would wake the watcher.
            rules_path = self.rules_file.resolve()

            def _relevant(_change, path_str: str) -> bool:
                path = Path(path_str).resolve()
                return path == rules_path or path.is_relative_to(mocks_dir)

            async for changes in awatch(
                *watch_paths,
                recursive=True,
                watch_filter=_relevant,
                debounce=0,
                stop_event=self._watcher_stop,
                rust_timeout=200,
                yield_on_timeout=True,
                ignore_permission_denied=True,
            ):
                if self._watcher_stop and self._watcher_stop.is_set():
                    break
                if not changes:
                    continue
                for _, path_str in changes:
                    path = Path(path_str).resolve()
                    if path == self.rules_file.resolve():
                        logger.info("Stunt: rules.yaml changed — reloading")
                        await self._reload()
                    elif path.is_relative_to(self.mocks_dir.resolve()):
                        async with self._lock:
                            self._mock_cache.clear()
                        logger.info("Stunt: mock file changed — cache cleared")
        except asyncio.CancelledError:
            logger.info("Stunt watcher cancelled")
        except Exception as e:
            if isinstance(e, PermissionError) or (isinstance(e, OSError) and getattr(e, "errno", None) == 13):
                logger.warning(f"Stunt watcher permission denied: {e}")
            else:
                logger.warning(f"Stunt watcher stopped: {e}")
        else:
            if self._watcher_stop and self._watcher_stop.is_set():
                logger.info("Stunt watcher cancelled")

    def running(self):
        if self.rules_file is None:
            self.rules_file = Path(ctx.options.stunt_rules)
        if self.mocks_dir is None:
            # An explicit --mocks wins; otherwise mocks/ sits beside the rules file
            # actually in use, not beside whatever _default_root() guessed.
            opt = ctx.options.stunt_mocks_dir
            explicit = opt != ctx.options.default("stunt_mocks_dir")
            self.mocks_dir = Path(opt) if explicit else self.rules_file.parent / "mocks"
        try:
            self.trace_cli = bool(ctx.options.stunt_trace)
        except AttributeError:
            pass
        self.trace = self.trace_cli or self.trace
        # Only create mocks/ where rules.yaml genuinely lives: a fallback discovery
        # guess landing in an unrelated directory must create nothing.
        if self.rules_file.exists():
            self.mocks_dir.mkdir(parents=True, exist_ok=True)
        else:
            logger.warning(
                f"Stunt: no rules file found at {self.rules_file} — "
                f"loading 0 rules. Pass --rules PATH or set STUNT_HOME to "
                f"point at your rules.yaml."
            )
        try:
            record_to = ctx.options.stunt_record
        except AttributeError:
            record_to = ""
        if record_to:
            self.start_recording(
                Path(record_to),
                host=ctx.options.stunt_record_host or None,
                path=ctx.options.stunt_record_path or None,
                force=ctx.options.stunt_record_force,
                raw=getattr(ctx.options, "stunt_record_raw", False),
            )
        loop = asyncio.get_running_loop()
        self._watcher_stop = asyncio.Event()
        self._watcher_task = loop.create_task(self._watcher())

    def start_recording(self, out, host=None, path=None, force=False, raw=False) -> bool:
        """Arm recording. Returns False, with a warning, if the target exists and
        --record-force was not given."""
        try:
            rec = Recorder(Path(out), self.mocks_dir or Path(out).parent / "mocks", host, path, force, raw)
        except re.error as e:
            logger.error(f"Stunt --record: invalid filter regex: {e}")
            return False
        if rec.refuses_clobber():
            logger.error(f"Stunt --record: {out} already exists — not recording " f"(pass --record-force to overwrite)")
            return False
        self._recorder = rec
        # Warn before the session, not just in the written file: whatever the backend
        # returns lands on disk looking like ordinary config.
        if raw:
            logger.warning(
                f"Stunt --record-raw: SCRUBBING IS OFF. Response bodies go to {out} verbatim — "
                f"tokens, session ids and personal data included, in cleartext. "
                f"Do not commit that file without reading every line of it."
            )
        else:
            logger.warning(
                f"Stunt --record: recording response bodies to {out}. Suspected secrets are "
                f"redacted, but scrubbing is best-effort — personal data and unrecognised tokens "
                f"can still land on disk. Review the file before committing it."
            )
        return True

    def _write_recording(self):
        if self._recorder is None:
            return
        rec, self._recorder = self._recorder, None
        try:
            for line in rec.write():
                print(line, file=sys.stderr)
        except OSError as e:
            logger.error(f"Stunt --record: could not write recording: {e}")

    async def aclose(self):
        self._write_recording()
        if self._watcher_stop:
            self._watcher_stop.set()
        if self._watcher_task and not self._watcher_task.done():
            self._watcher_task.cancel()
            try:
                await self._watcher_task
            except asyncio.CancelledError:
                pass

    def done(self):
        # Write synchronously: done() runs while the event loop is torn down, so the
        # recording must not depend on the aclose() task being scheduled.
        self._write_recording()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        loop.create_task(self.aclose())

    async def request(self, flow: http.HTTPFlow):
        await self._process(flow, True)
        await self._throttle(flow)

    async def response(self, flow: http.HTTPFlow):
        # Observe before _process, so a recording captures what the backend sent
        # rather than what a modify_response_* rule turned it into.
        if self._recorder is not None:
            self._recorder.observe(flow)
        await self._process(flow, False)
        await self._throttle(flow)

    # ------------------------------------------------------------------ #
    # mitmproxy command surface. Registered commands appear in the mitmweb
    # command palette and the mitmproxy console, which is the whole GUI.
    #
    # mitmproxy calls these handlers synchronously on the loop thread, and no
    # `async with self._lock` block in this addon awaits, so the lock is always
    # free and the state consistent here. Handlers therefore touch
    # _state/_rule_hits/rules directly; a sync handler cannot await the lock.
    # ------------------------------------------------------------------ #

    def _rule_status(self, rule: "CompiledRule") -> str:
        hits = self._rule_hits.get(rule.name, 0)
        if rule.once and hits >= 1:
            return "exhausted (once)"
        if rule.count is not None and hits >= rule.count:
            return f"exhausted ({hits}/{rule.count})"
        if rule.state_require and not self._state_holds(self._state, rule):
            return "gated (state.require)"
        return "active"

    @command.command("stunt.rules.list")
    def cmd_rules_list(self) -> AbcSequence[str]:
        """List loaded rules in priority order with hit counts and status."""
        rows = self._rules_all
        if not rows:
            return [f"Stunt: 0 rules loaded (rules file: {self.rules_file})"]
        out = [f"Stunt: {len(rows)} rule(s), priority order:"]
        width = max(len(r.name) for r in rows)
        for rule in rows:
            enabled = rule.name not in self._disabled
            hits = self._rule_hits.get(rule.name, 0)
            status = self._rule_status(rule) if enabled else "DISABLED (session)"
            mark = "*" if enabled else "-"
            out.append(f"  {mark} {rule.name:<{width}}  prio={rule.priority:<4} hits={hits:<4} {status}")
        return out

    @command.command("stunt.rules.toggle")
    def cmd_rules_toggle(self, name: str) -> str:
        """Enable/disable a rule by name for this session only. In-memory:
        rules.yaml is untouched and the toggle is lost on the next reload."""
        if name in self._disabled:
            del self._disabled[name]
            verdict = f"Stunt: rule '{name}' ENABLED (session only)"
        elif any(r.name == name for r in self._rules_all):
            self._disabled[name] = True
            verdict = (
                f"Stunt: rule '{name}' DISABLED for this session only — "
                f"rules.yaml unchanged, and the next reload restores it"
            )
        else:
            return f"Stunt: no rule named '{name}'"
        self.rules = tuple(r for r in self._rules_all if r.name not in self._disabled)
        return verdict

    @command.command("stunt.state.get")
    def cmd_state_get(self) -> AbcSequence[str]:
        """Show the current Stunt state dict."""
        if not self._state:
            return ["Stunt state: (empty)"]
        return [f"Stunt state ({len(self._state)} key(s)):"] + [
            f"  {k} = {json.dumps(v, default=str)}" for k, v in sorted(self._state.items(), key=lambda kv: str(kv[0]))
        ]

    @command.command("stunt.state.set")
    def cmd_state_set(self, key: str, value: str) -> str:
        """Set a state key. The value is parsed as JSON when possible (so 2 is
        the number 2, true is a bool), otherwise kept as a string."""
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = value
        self._state[key] = parsed
        return f"Stunt state: {key} = {json.dumps(parsed, default=str)}"

    @command.command("stunt.record.start")
    def cmd_record_start(self, out: str, host: str = "", path: str = "") -> str:
        """Start recording observed traffic to a rules file, flushed by
        stunt.record.stop. Optional host/path regex filters."""
        if self._recorder is not None:
            return "Stunt: already recording — run stunt.record.stop first"
        if self.start_recording(Path(out), host=host or None, path=path or None):
            return (
                f"Stunt: recording to {out} — suspected secrets are redacted, but "
                f"scrubbing is best-effort; review before committing. "
                f"Run stunt.record.stop to flush."
            )
        return f"Stunt: could not start recording to {out} — see the log"

    @command.command("stunt.record.stop")
    def cmd_record_stop(self) -> str:
        """Stop recording and write the rules file now."""
        if self._recorder is None:
            return "Stunt: not recording"
        out = self._recorder.out
        self._write_recording()
        return f"Stunt: recording flushed to {out}"

    @command.command("stunt.reload")
    def cmd_reload(self) -> str:
        """Force a reload of rules.yaml, even if the file has not changed."""
        self._hash = ""
        coro = self._reload()
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            asyncio.run(coro)
        return f"Stunt: reloading {self.rules_file}"


# Keys that narrow a rule to a subset of requests. A rule with none of them
# matches everything, which lint flags.
_MATCHER_KEYS = (
    "method",
    "host",
    "url_regex",
    "url_contains",
    "path_regex",
    "path_contains",
    "status_code",
    "header_contains",
    "query_contains",
    "json_body",
    "response_json",
    "json_contains",
    "response_json_contains",
)


def lint_rules(rules_path: Path, mocks_dir: Path) -> List[str]:
    """Validate a rules file with no proxy running.

    Runs the live addon's YAML parse + _compile_rules + _warn_unknown_keys pipeline
    and captures what it logs, so lint and runtime cannot disagree.

    Returns a list of problem strings; empty means clean.
    """
    if not rules_path.is_file():
        return [f"rules file not found: {rules_path}"]
    content = rules_path.read_bytes()
    try:
        data = yaml.safe_load(content) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        loc = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return [f"YAML syntax error{loc}: {e}"]
    if not isinstance(data, dict):
        return ["rules file must contain a YAML mapping at the top level"]

    problems: List[str] = []
    captured: List[str] = []
    handler = logging.Handler()
    handler.setLevel(logging.WARNING)
    handler.emit = lambda record: captured.append(record.getMessage())
    logger.addHandler(handler)
    try:
        _warn_unknown_keys(data, TOP_LEVEL_KEYS, "top-level")
        compiled = Stunt._compile_rules(data)
    finally:
        logger.removeHandler(handler)
    problems.extend(captured)

    # Valid config, so _compile_rules accepts it; only lint objects.
    defaults = data.get("defaults") if isinstance(data.get("defaults"), dict) else {}
    raw_rules = list(data.get("rules") or []) + _desugar_mock(data.get("mock"))
    for idx, raw in enumerate(raw_rules):
        if not isinstance(raw, dict):
            continue
        merged = {**defaults, **raw} if defaults else raw
        if not merged.get("enabled", True):
            continue
        name = merged.get("name", f"unnamed_rule_{idx + 1}")
        if not any(merged.get(k) for k in _MATCHER_KEYS):
            problems.append(f"Rule '{name}': no matchers — this rule matches every request")

    # Referenced mock files must exist.
    for rule in compiled:
        variants = (rule.respond_with, rule.inject_error, *rule.respond_with_cycle, *rule.respond_with_random)
        for variant in variants:
            if not isinstance(variant, dict):
                continue
            file_val = variant.get("file")
            if file_val and isinstance(file_val, str) and not (mocks_dir / file_val).is_file():
                problems.append(f"Rule '{rule.name}': mock file not found: {mocks_dir / file_val}")

    return problems


# Recording is CLI-only (`--record PATH`, `--record-host`, `--record-path`): it
# WRITES a rules file, so a switch living inside one would arm itself on hot reload
# and overwrite the file it was read from.
#
# Bodies at or under this size are inlined into rules.yaml when they parse as JSON;
# anything larger, or not JSON, goes to mocks/ and is referenced with `file:`.
RECORD_INLINE_MAX = 2048
# Bodies up to this size are parsed as JSON so they can be scrubbed key-by-key,
# inlined or not. Past it, only the regex fallback applies.
RECORD_PARSE_MAX = 4 * 1024 * 1024

# Best-effort secret redaction for recorded bodies. Three deliberately narrow
# detectors; over-eager redaction would make recordings useless:
#   1. sensitive JSON key -> redact the value (structure-aware, no regex)
#   2. JWT shape          -> redact anywhere it appears
#   3. long opaque blob   -> redact only when ALL of: >=40 chars, token charset,
#      upper+lower+digit, Shannon entropy >= 3.5 bits/char. The mixed-case rule
#      is what spares hex digests, UUIDs and slugs.
REDACTED = "<redacted>"
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")
_SENSITIVE_KEYS = frozenset("""
    accesstoken refreshtoken idtoken token tokens accesskey secretkey apikey apisecret apitoken
    authtoken authorization auth password passwd pwd secret secrets clientsecret privatekey
    session sessionid sessiontoken sessionkey cookie setcookie credential credentials jwt bearer
    xapikey csrftoken xsrftoken
""".split())
_SENSITIVE_SUFFIXES = ("token", "secret", "password", "apikey", "privatekey")
_OPAQUE_RE = re.compile(r"[A-Za-z0-9_\-+/=.]{40,}")
_ENTROPY_MIN = 3.5


def _sensitive_key(key: str) -> bool:
    k = re.sub(r"[^a-z0-9]", "", key.lower())
    return k in _SENSITIVE_KEYS or k.endswith(_SENSITIVE_SUFFIXES)


def _high_entropy(s: str) -> bool:
    if len(s) < 40 or not _OPAQUE_RE.fullmatch(s):
        return False
    if not (any(c.islower() for c in s) and any(c.isupper() for c in s) and any(c.isdigit() for c in s)):
        return False  # spares hex digests, UUIDs, lowercase slugs
    counts = Counter(s)
    entropy = -sum((n / len(s)) * math.log2(n / len(s)) for n in counts.values())
    return entropy >= _ENTROPY_MIN


def _scrub_text(text: str) -> Tuple[str, int]:
    """Regex fallback for non-JSON bodies: JWTs and long opaque blobs."""
    n = 0

    def sub(m):
        nonlocal n
        n += 1
        return REDACTED

    text = _JWT_RE.sub(sub, text)
    text = _OPAQUE_RE.sub(lambda m: sub(m) if _high_entropy(m.group(0)) else m.group(0), text)
    return text, n


def _scrub_json(value: Any, counter: List[int], key: Optional[str] = None) -> Any:
    if isinstance(value, dict):
        return {k: _scrub_json(v, counter, k if isinstance(k, str) else None) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_json(v, counter, key) for v in value]
    if (
        key is not None
        and _sensitive_key(key)
        and isinstance(value, (str, int, float))
        and not isinstance(value, bool)
        and value != ""
    ):
        counter[0] += 1
        return REDACTED
    if isinstance(value, str):
        if _JWT_RE.search(value) or _high_entropy(value):
            scrubbed, n = _scrub_text(value)
            counter[0] += n
            return scrubbed
    return value


def _scrub_bytes(body: bytes) -> Tuple[bytes, int]:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body, 0  # binary: nothing text-shaped to find
    scrubbed, n = _scrub_text(text)
    return scrubbed.encode("utf-8"), n


class Recorder:
    """Collect responses seen on the wire and write them out as rules.

    Content-Type is the only header carried over. Being an allowlist, it excludes
    hop-by-hop and sensitive headers by construction.
    """

    def __init__(
        self,
        out: Path,
        mocks_dir: Path,
        host: Optional[str] = None,
        path: Optional[str] = None,
        force: bool = False,
        raw: bool = False,
    ):
        self.out = Path(out)
        self.mocks_dir = Path(mocks_dir)
        self.force = force
        # Scrub by default; `raw` is the deliberate opt-out (--record-raw).
        self.raw = raw
        self.redactions = 0
        self.host_re = re.compile(host) if host else None
        self.path_re = re.compile(path) if path else None
        # (host, METHOD, path) -> entry. First response for a key wins, so a replay
        # reproduces what the app saw first rather than the last cache-buster.
        self.entries: "OrderedDict[Tuple[str, str, str], dict]" = OrderedDict()
        self._names: Dict[str, bytes] = {}

    def refuses_clobber(self) -> bool:
        return self.out.exists() and not self.force

    def observe(self, flow: http.HTTPFlow):
        resp = flow.response
        if resp is None or flow.metadata.get("stunt_handled", False):
            return  # nothing real to record — that response is Stunt's own
        req = flow.request
        host = req.host or ""
        path = (req.path or "/").split("?", 1)[0]
        if self.host_re and not self.host_re.search(host):
            return
        if self.path_re and not self.path_re.search(path):
            return
        key = (host, req.method.upper(), path)
        if key in self.entries:
            return
        body = resp.content or b""
        ct = (resp.headers.get("content-type") or "").strip()
        # Parse for SCRUBBING first, and only then decide inline vs file:. Tying the
        # two together would skip the structure-aware scrub on large bodies.
        data = None
        if "json" in ct.lower() and len(body) <= RECORD_PARSE_MAX:
            try:
                data = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                data = None
        if not self.raw:
            if data is not None:
                counter = [0]
                data = _scrub_json(data, counter)
                self.redactions += counter[0]
                if counter[0]:
                    # Compact, so scrubbing alone cannot push a body over
                    # RECORD_INLINE_MAX and out to a file.
                    body = json.dumps(data).encode()
            else:
                body, n = _scrub_bytes(body)
                self.redactions += n
        respond_with: Dict[str, Any] = {"status": resp.status_code}
        if data is not None and len(body) <= RECORD_INLINE_MAX:
            respond_with["body"] = data
        else:
            respond_with["file"] = self._filename(key, ct, body)
            # respond_with defaults a JSON body to application/json; only other
            # types need it spelled out, which forces a full `rules:` entry.
            if "json" not in ct.lower():
                respond_with["headers"] = {"Content-Type": ct or "application/octet-stream"}
        self.entries[key] = respond_with

    def _filename(self, key: Tuple[str, str, str], ct: str, body: bytes) -> str:
        _, method, path = key
        ext = mimetypes.guess_extension(ct.split(";")[0].strip()) or ".bin"
        slug = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_") or "root"
        base = f"rec_{method.lower()}_{slug}"[:80]
        name = f"{base}{ext}"
        n = 2
        while name in self._names:
            name, n = f"{base}_{n}{ext}", n + 1
        self._names[name] = body
        return name

    def write(self) -> List[str]:
        """Write rules + mock files. Returns human-readable report lines."""
        if not self.entries:
            return [f"stunt --record: recorded 0 responses — {self.out} not written"]
        if self.refuses_clobber():
            return [
                f"stunt --record: refusing to overwrite {self.out} — "
                f"nothing written (pass --record-force to overwrite)"
            ]

        # One host -> the `mock:` shorthand is unambiguous. Several hosts share a
        # path space, so each rule needs an explicit `host:`, which the shorthand
        # cannot express.
        multi_host = len({k[0] for k in self.entries}) > 1
        mock: Dict[str, Any] = {}
        rules: List[dict] = []
        for (host, method, path), rw in self.entries.items():
            plain = rw["status"] == 200 and "headers" not in rw and not multi_host
            if plain and "body" in rw:
                value = rw["body"]
                # `mock:` reads an all-respond_with-keys dict as a respond_with
                # object; wrap such a body so it stays a body.
                if (
                    isinstance(value, dict)
                    and value
                    and not (value.keys() - RESPOND_WITH_KEYS)
                    and isinstance(value.get("status", 200), int)
                ):
                    value = {"body": value}
                mock[f"{method} {path}"] = value
            elif plain:
                mock[f"{method} {path}"] = {"file": rw["file"]}
            else:
                rule: Dict[str, Any] = {"name": f"recorded {method} {host}{path}"}
                if multi_host:
                    rule["host"] = f"^{re.escape(host)}$"
                rule["method"] = method
                rule["path_regex"] = f"^{re.escape(path)}(\\?.*)?$"
                rule["respond_with"] = rw
                rules.append(rule)

        config: Dict[str, Any] = {}
        if mock:
            config["mock"] = mock
        if rules:
            config["rules"] = rules
        if self.raw:
            header_note = "# NOT scrubbed (--record-raw): response bodies are verbatim, secrets included.\n"
        else:
            header_note = (
                f"# Scrubbed by --record: {self.redactions} value(s) replaced with "
                f'"{REDACTED}". Scrubbing is best-effort.\n'
            )
        header = (
            "# yaml-language-server: $schema=./docs/stunt_schema.json\n"
            "# Recorded by `stunt --record`. Review before committing: a recording\n"
            "# can still contain personal data, tokens or IDs inside response bodies.\n" + header_note
        )
        self.out.parent.mkdir(parents=True, exist_ok=True)
        self.out.write_text(header + yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
        report = [
            f"stunt --record: wrote {self.out} "
            f"({len(mock)} mock entr{'y' if len(mock) == 1 else 'ies'}, {len(rules)} rule(s))"
        ]
        if self.raw:
            report.append(
                "stunt --record: NOT scrubbed (--record-raw) — bodies are verbatim, "
                "secrets included. Do not commit this file without reading it."
            )
        else:
            report.append(
                f"stunt --record: redacted {self.redactions} suspected secret value(s). "
                f"Scrubbing is best-effort — review the file before committing."
            )
        if self._names:
            self.mocks_dir.mkdir(parents=True, exist_ok=True)
            for name, body in self._names.items():
                (self.mocks_dir / name).write_bytes(body)
            report.append(f"stunt --record: wrote {len(self._names)} body file(s) to {self.mocks_dir}")
        return report


def load(loader):
    root = _default_root()
    loader.add_option(
        name="stunt_rules",
        typespec=str,
        default=str((root / "rules.yaml").resolve()),
        help="Path to rules.yaml",
    )
    loader.add_option(
        name="stunt_mocks_dir",
        typespec=str,
        default=str((root / "mocks").resolve()),
        help="Directory for mock files",
    )
    loader.add_option(
        name="stunt_trace",
        typespec=bool,
        default=False,
        help="Log which rules were considered per request and why each one didn't match.",
    )
    loader.add_option(
        name="stunt_record",
        typespec=str,
        default="",
        help="Record observed traffic to this rules file on shutdown.",
    )
    loader.add_option(
        name="stunt_record_host",
        typespec=str,
        default="",
        help="Only record hosts matching this regex (same dialect as the `host` matcher).",
    )
    loader.add_option(
        name="stunt_record_path",
        typespec=str,
        default="",
        help="Only record paths matching this regex (same dialect as `path_regex`).",
    )
    loader.add_option(
        name="stunt_record_force",
        typespec=bool,
        default=False,
        help="Allow --record to overwrite an existing rules file.",
    )
    loader.add_option(
        name="stunt_record_raw",
        typespec=bool,
        default=False,
        help="Disable secret scrubbing — record response bodies verbatim.",
    )


addons = [Stunt()]
