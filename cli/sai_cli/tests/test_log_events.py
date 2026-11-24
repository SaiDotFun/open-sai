"""Check that the event registry covers production calls and generated docs."""

from __future__ import annotations

import ast
from pathlib import Path

from sai_cli.core.log_events import (
    COMMON_OPTIONAL,
    EVENTS,
    render_json,
    render_markdown,
)

PACKAGE = Path(__file__).resolve().parents[1]
DOCS = PACKAGE.parents[1] / "docs"


def test_log_event_docs_are_current() -> None:
    assert (DOCS / "log-events.json").read_text(encoding="utf-8") == render_json(), (
        "Run just log-events"
    )
    assert (DOCS / "log-events.md").read_text(encoding="utf-8") == render_markdown(), (
        "Run just log-events"
    )


def test_log_event_calls_match_registry() -> None:
    violations = []
    for path in sorted(PACKAGE.rglob("*.py")):
        relative = path.relative_to(PACKAGE)
        if relative.parts[0] == "tests":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "event"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in {"log", "sai_log"}
            ):
                continue
            where = f"{relative}:{node.lineno}"
            name = (
                node.args[0].value
                if node.args and isinstance(node.args[0], ast.Constant)
                else None
            )
            if not isinstance(name, str) or name not in EVENTS:
                violations.append(f"{where}: unregistered event {name!r}")
                continue
            spec = EVENTS[name]
            provided = {kw.arg for kw in node.keywords if kw.arg is not None}
            if len(provided) != len(node.keywords):
                violations.append(f"{where}: {name} needs explicit keyword arguments")
            unknown = (
                provided
                - spec.required.keys()
                - spec.optional.keys()
                - COMMON_OPTIONAL.keys()
            )
            missing = spec.required.keys() - provided - COMMON_OPTIONAL.keys()
            if unknown:
                violations.append(
                    f"{where}: {name} has unregistered fields {sorted(unknown)}"
                )
            if missing:
                violations.append(
                    f"{where}: {name} is missing required fields {sorted(missing)}"
                )
    assert not violations, "\n".join(violations)
