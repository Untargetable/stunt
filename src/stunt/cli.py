import argparse
import os
import subprocess
import sys
from pathlib import Path

# Schema URL, not a relative path: scaffolded and recorded files land in the user's
# own directory, which has no docs/ tree. Keep in sync with addon.SCHEMA_URL.
SCHEMA_URL = "https://raw.githubusercontent.com/Untargetable/stunt/main/docs/stunt_schema.json"

# Template written by `stunt init`. Not an f-string: the body is full of YAML braces.
_STARTER_RULES_YAML = f"# yaml-language-server: $schema={SCHEMA_URL}\n" + """\
# Scaffolded by `stunt init`. Edit freely — rules.yaml hot-reloads on save.

mock:
  # Bare value -> 200 JSON body.
  GET /api/status: {status: "ok"}

  # `file:` serves mocks/example_user.json verbatim — handy for larger fixtures.
  GET /api/users/1: {file: "example_user.json"}

rules: []
"""

_STARTER_MOCK_JSON = """\
{
  "id": 1,
  "name": "Ada Lovelace",
  "email": "ada@example.com"
}
"""


def _cmd_init(args) -> int:
    rules_path = Path(args.rules).expanduser().resolve() if args.rules else Path.cwd() / "rules.yaml"
    mocks_path = Path(args.mocks).expanduser().resolve() if args.mocks else Path.cwd() / "mocks"
    example_mock = mocks_path / "example_user.json"

    created, skipped = [], []

    def write(path: Path, content: str):
        if path.exists() and not args.force:
            skipped.append(path)
        else:
            path.write_text(content)
            created.append(path)

    write(rules_path, _STARTER_RULES_YAML)
    mocks_path.mkdir(parents=True, exist_ok=True)
    write(example_mock, _STARTER_MOCK_JSON)

    for path in created:
        print(f"created {path}")
    for path in skipped:
        print(f"skipped {path} (already exists — use --force to overwrite)")

    print()
    print("Next steps:")
    print("  1. Start the proxy:      stunt            (mitmweb; --runner proxy|dump for others)")
    print("  2. Trust the CA cert:    visit http://mitm.it through the proxy, once")
    print("  3. Try it:               curl -x http://localhost:8080 http://api.example.com/api/status")
    return 0


def _cmd_lint(args) -> int:
    # Local import: addon.py pulls in mitmproxy/jsonpath_ng/watchfiles, which the
    # run path shells out to rather than importing.
    from stunt.addon import _default_root, lint_rules

    root = _default_root()
    rules_path = Path(args.rules).expanduser().resolve() if args.rules else root / "rules.yaml"
    # mocks/ sits beside the rules file being linted, matching what the addon
    # resolves at runtime.
    mocks_path = Path(args.mocks).expanduser().resolve() if args.mocks else rules_path.parent / "mocks"

    problems = lint_rules(rules_path, mocks_path)
    for problem in problems:
        print(f"- {problem}")
    if problems:
        print(f"\n{len(problems)} problem(s) found in {rules_path}")
        return 1
    print(f"OK: {rules_path}")
    return 0


def main():
    # argparse routes anything after a subcommand token to that subparser, so the
    # shared --rules/--mocks flags are given to both via `parents=`.
    paths_parser = argparse.ArgumentParser(add_help=False)
    paths_parser.add_argument(
        "--rules",
        help="Path to rules.yaml, overriding discovery ($STUNT_HOME / cwd walk-up).",
    )
    paths_parser.add_argument(
        "--mocks",
        help="Path to the mocks directory, overriding discovery.",
    )

    parser = argparse.ArgumentParser(
        prog="stunt",
        description="Run mitmproxy with the Stunt addon.",
        add_help=True,
        parents=[paths_parser],
    )
    parser.add_argument(
        "--runner",
        choices=["web", "proxy", "dump"],
        default=None,
        help="Runner to use: web (mitmweb), proxy (mitmproxy), dump (mitmdump).",
    )
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Deprecated alias for --runner when set to web/proxy/dump (prints a warning). "
            "Any other value (reverse:URL, transparent, socks5, upstream:URL, ...) is "
            "mitmproxy's own --mode and is forwarded to it untouched."
        ),
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help=(
            "Record the traffic seen during this session and write it to PATH as a "
            "ready-to-use rules file when the proxy stops. Off unless given."
        ),
    )
    parser.add_argument("--record-host", metavar="REGEX", help="Only record hosts matching this regex.")
    parser.add_argument("--record-path", metavar="REGEX", help="Only record paths matching this regex.")
    parser.add_argument("--record-force", action="store_true", help="Let --record overwrite an existing rules file.")
    parser.add_argument(
        "--record-raw",
        action="store_true",
        help=(
            "Disable secret scrubbing: write response bodies verbatim. Recordings are scrubbed "
            "by default; use this only when you need the real payload and will not commit it."
        ),
    )
    parser.add_argument(
        "--trace-matches",
        action="store_true",
        help="Log which rules were considered per request and why each one didn't match.",
    )

    # Register subcommands only when one is actually being invoked: otherwise
    # argparse reads the value of a pass-through flag as a positional and rejects
    # it against the subcommand choices (`stunt --listen-port 8081`).
    argv = sys.argv[1:]
    subcommand = argv[0] if argv and argv[0] in ("init", "lint") else None

    if subcommand:
        sub = parser.add_subparsers(dest="command")
        p_init = sub.add_parser(
            "init", help="Scaffold rules.yaml and mocks/ in the current directory.", parents=[paths_parser]
        )
        p_init.add_argument("--force", action="store_true", help="Overwrite existing files.")
        sub.add_parser("lint", help="Validate a rules file without starting a proxy.", parents=[paths_parser])
        args = parser.parse_args(argv)
        sys.exit(_cmd_init(args) if subcommand == "init" else _cmd_lint(args))

    args, passthrough = parser.parse_known_args(argv)

    runner_choice = args.runner
    mode_forward = None
    if args.mode is not None:
        if args.mode in ("web", "proxy", "dump"):
            print(
                "warning: --mode for runner selection is deprecated, use --runner instead",
                file=sys.stderr,
            )
            runner_choice = runner_choice or args.mode
        else:
            # mitmproxy's own --mode (reverse:URL, transparent, socks5, upstream:URL, ...)
            mode_forward = args.mode
    if runner_choice is None:
        runner_choice = os.environ.get("STUNT_MODE", "web")

    addon_path = Path(__file__).parent / "addon.py"
    runner = {
        "web": "mitmweb",
        "proxy": "mitmproxy",
        "dump": "mitmdump",
    }[runner_choice]

    cmd = [runner, "-s", str(addon_path)]
    if args.rules:
        cmd += ["--set", f"stunt_rules={Path(args.rules).expanduser().resolve()}"]
    if args.mocks:
        cmd += ["--set", f"stunt_mocks_dir={Path(args.mocks).expanduser().resolve()}"]
    if args.trace_matches:
        cmd += ["--set", "stunt_trace=true"]
    if args.record:
        cmd += ["--set", f"stunt_record={Path(args.record).expanduser().resolve()}"]
    if args.record_host:
        cmd += ["--set", f"stunt_record_host={args.record_host}"]
    if args.record_path:
        cmd += ["--set", f"stunt_record_path={args.record_path}"]
    if args.record_force:
        cmd += ["--set", "stunt_record_force=true"]
    if args.record_raw:
        cmd += ["--set", "stunt_record_raw=true"]
    if mode_forward:
        cmd += ["--mode", mode_forward]
    cmd += passthrough

    try:
        result = subprocess.run(cmd, check=False)
    except FileNotFoundError:
        # Almost always a venv that isn't on PATH; a traceback would not say so.
        print(
            f"stunt: '{runner}' not found on PATH.\n"
            f"  It ships with mitmproxy, which is a dependency of stunt.\n"
            f"  Activate the virtualenv you installed into, or reinstall with:\n"
            f"      pip install -e .",
            file=sys.stderr,
        )
        sys.exit(127)
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
