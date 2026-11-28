"""Enumerate the CLI installed in the interpreter running this file."""

import json
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import click


def cli_commands(group: click.Group, prefix: str = "") -> set[str]:
    result = set()
    for name, command in group.commands.items():
        full = f"{prefix} {name}".strip()
        if isinstance(command, click.Group):
            result.update(cli_commands(command, full))
        else:
            result.add(full)
    return result


def require_manifest_case_ids(names: Iterable[str], inventory: Path) -> None:
    configured = Counter(names)
    duplicates = sorted(name for name, count in configured.items() if count > 1)
    if duplicates:
        raise ValueError(f"Duplicate manifest case IDs: {', '.join(duplicates)}")
    required = {
        name
        for name in inventory.read_text().splitlines()
        if name and not name.startswith("#")
    }
    if not required:
        raise ValueError("Release case inventory is empty")
    if missing := required - configured.keys():
        raise ValueError(
            f"Missing required release case IDs: {', '.join(sorted(missing))}"
        )


if __name__ == "__main__":
    from sai_cli.sai_cli import main

    print(json.dumps(sorted(cli_commands(main))))
