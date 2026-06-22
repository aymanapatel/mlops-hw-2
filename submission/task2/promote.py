"""scripts/promote.py — promote MLflow Registry aliases with an audit log.

YOUR TASK (see tasks/task2.md): implement the four subcommand functions.
The argparse scaffolding below is wired so each cmd_* receives an `args`
namespace already parsed. See `_build_parser` for what's on `args` per
subcommand, and tasks/task2.md "Behavioral specs" for what each function
must do.

Versions are identified by their `config_id` tag (e.g., "v6"), NOT by
MLflow's integer version numbers. If multiple versions match, the CLI
warns and uses the highest MLflow version number.

Successful `set` and `rollback` operations append a JSON event to
LOG_FILE (promotion-log.jsonl at repo root). `rollback` consults the
log to find the previous alias target.

Subcommands:
  set <alias> <config_id>   move alias, append `set` event to the log
  show <alias>              print current target + tags + key metrics
  list                      print all aliases on the registered model
  rollback <alias>          move alias back per the audit log, append
                            `rollback` event
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import mlflow
from mlflow.exceptions import MlflowException, RestException
from mlflow.tracking import MlflowClient

from src.config import get_settings

REGISTERED_MODEL_NAME = "travel-assistant"
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = (
    SCRIPT_DIR.parent / "promotion-log.jsonl"
    if SCRIPT_DIR.name == "scripts"
    else SCRIPT_DIR / "promotion-log.jsonl"
)


def _client() -> MlflowClient:
    settings = get_settings()
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    return MlflowClient()


def _fail(message: str) -> None:
    print(f"error: {message}")
    sys.exit(1)


def _quote_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _config_id(mv) -> str:
    return (mv.tags or {}).get("config_id", "")


def _find_version_by_config_id(
    client: MlflowClient,
    model_name: str,
    config_id: str,
):
    model_filter = _quote_filter_value(model_name)
    config_filter = _quote_filter_value(config_id)
    filters = [
        f"name = '{model_filter}' AND tags.config_id = '{config_filter}'",
        f"name = '{model_filter}' AND tag.config_id = '{config_filter}'",
    ]

    matches = []
    for filter_string in filters:
        try:
            matches = list(client.search_model_versions(filter_string))
        except MlflowException:
            continue
        if matches:
            break

    if not matches:
        try:
            versions = list(client.search_model_versions(f"name = '{model_filter}'"))
        except MlflowException:
            versions = []
        matches = [mv for mv in versions if _config_id(mv) == config_id]

    if not matches:
        _fail(f"no version found with config_id={config_id}")

    matches.sort(key=lambda mv: int(mv.version))
    if len(matches) > 1:
        versions = [int(mv.version) for mv in matches]
        latest = versions[-1]
        print(
            f"warning: multiple versions match config_id={config_id} "
            f"(MLflow versions {versions}); using latest ({latest})"
        )
    return matches[-1]


def _get_alias_target(client: MlflowClient, model_name: str, alias: str):
    try:
        return client.get_model_version_by_alias(model_name, alias)
    except RestException:
        return None


def _append_log(alias: str, from_config: str, to_config: str, op: str) -> None:
    event = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "alias": alias,
        "from": from_config,
        "to": to_config,
        "op": op,
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def _read_log() -> list[dict]:
    if not LOG_FILE.exists():
        return []

    events = []
    for line in LOG_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _format_config(config_id: str) -> str:
    return config_id or "(unset)"


def cmd_set(args: argparse.Namespace) -> None:
    """args.alias: str, args.config_id: str. See tasks/task2.md → cmd_set."""
    client = _client()
    target = _find_version_by_config_id(client, args.name, args.config_id)
    current = _get_alias_target(client, args.name, args.alias)
    current_config = _config_id(current) if current is not None else ""

    client.set_registered_model_alias(args.name, args.alias, target.version)
    _append_log(args.alias, current_config, args.config_id, "set")

    print(f"{args.alias}: {_format_config(current_config)} → {args.config_id}")


def cmd_show(args: argparse.Namespace) -> None:
    """args.alias: str. See tasks/task2.md → cmd_show."""
    client = _client()
    mv = _get_alias_target(client, args.name, args.alias)
    if mv is None:
        _fail(f"alias {args.alias} is not set")

    metrics = client.get_run(mv.run_id).data.metrics if mv.run_id else {}
    tags = mv.tags or {}

    print(f"{args.name} @ {args.alias}")
    print(f"  config_id: {tags.get('config_id', '')}")
    for key in sorted(k for k in tags if k != "config_id"):
        print(f"  {key}: {tags[key]}")
    if "accuracy_overall" in metrics:
        print(f"  accuracy_overall: {metrics['accuracy_overall']}")
    print(f"  verdict_rate_leaked: {metrics.get('verdict_rate_leaked', 0.0)}")
    if "total_cost_usd" in metrics:
        print(f"  total_cost_usd: ${metrics['total_cost_usd']:.2f}")


def cmd_list(args: argparse.Namespace) -> None:
    """No args. See tasks/task2.md → cmd_list."""
    client = _client()
    try:
        model = client.get_registered_model(args.name)
    except MlflowException:
        print("no aliases set")
        return

    aliases = getattr(model, "aliases", None) or {}
    if not aliases:
        print("no aliases set")
        return

    alias_names = sorted(aliases)
    width = max(len(alias) for alias in alias_names)
    for alias in alias_names:
        mv = client.get_model_version(args.name, aliases[alias])
        print(f"{alias.ljust(width)} -> {_config_id(mv)}")


def cmd_rollback(args: argparse.Namespace) -> None:
    """args.alias: str. See tasks/task2.md → cmd_rollback."""
    client = _client()
    current = _get_alias_target(client, args.name, args.alias)
    if current is None:
        _fail("nothing to roll back")

    current_config = _config_id(current)
    entries = [e for e in _read_log() if e.get("alias") == args.alias]
    if not entries:
        _fail(f"no promotion history for alias {args.alias}")

    previous_event = entries[-1]
    if previous_event.get("op") == "rollback":
        _fail(f"{args.alias} was just rolled back; no further history to walk back to")

    previous_config = previous_event.get("from", "")
    if not previous_config:
        _fail(f"{args.alias} has no previous target (first promotion ever)")

    target = _find_version_by_config_id(client, args.name, previous_config)
    client.set_registered_model_alias(args.name, args.alias, target.version)
    _append_log(args.alias, current_config, previous_config, "rollback")

    print(f"{args.alias}: {current_config} → {previous_config} (rolled back)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--name",
        default=REGISTERED_MODEL_NAME,
        help=f"Registered model name (default: {REGISTERED_MODEL_NAME})",
    )

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser(
        "set", help="Move an alias to a version (by config_id), append a set event"
    )
    p_set.add_argument("alias", help="Alias to assign (e.g., 'production')")
    p_set.add_argument(
        "config_id",
        help=(
            "Config identifier (e.g., 'v6') — resolved via the config_id tag on registered versions"
        ),
    )
    p_set.set_defaults(func=cmd_set)

    p_show = sub.add_parser("show", help="Show which version an alias points at")
    p_show.add_argument("alias")
    p_show.set_defaults(func=cmd_show)

    p_list = sub.add_parser("list", help="List all aliases on the registered model")
    p_list.set_defaults(func=cmd_list)

    p_rollback = sub.add_parser(
        "rollback",
        help="Move an alias back to its previous target per the audit log",
    )
    p_rollback.add_argument("alias")
    p_rollback.set_defaults(func=cmd_rollback)

    return parser


def main() -> None:
    args = _build_parser().parse_args()
    try:
        args.func(args)
    except NotImplementedError as exc:
        print(f"NOT IMPLEMENTED: {exc}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
