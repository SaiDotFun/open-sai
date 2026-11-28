"""Classify full release comparisons against reviewed, row-specific signatures."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from release_mismatch import difference_signature


@dataclass(frozen=True)
class Gap:
    owner: str
    signature: str
    title: str = ""


class ManifestGaps:
    def __init__(
        self,
        path: Path,
        *,
        case_ids: set[str] | None = None,
        row_label: str = "CUTOVER_ROW",
    ) -> None:
        with path.open("rb") as file:
            self.gaps = {
                name: Gap(**entry)
                for name, entry in tomllib.load(file)["gap"].items()
                if case_ids is None or name in case_ids
            }
        for name, gap in self.gaps.items():
            if (
                not name.startswith(("case:", "cli:", "mcp:"))
                or not re.fullmatch(r"[a-f0-9]{64}", gap.signature)
                or not (
                    re.fullmatch(r"#[1-9][0-9]*", gap.owner)
                    or (gap.owner == "M2-unassigned" and gap.title.strip())
                )
            ):
                raise ValueError(f"Invalid release gap entry for {name}")
        self.seen: set[str] = set()
        self.failed = False
        self.row_label = row_label

    def result(self, name: str, differences: list[str]) -> tuple[str, str]:
        if name in self.seen:
            raise ValueError(f"Duplicate cutover row {name}")
        self.seen.add(name)
        gap = self.gaps.get(name)
        signature = difference_signature(differences)
        if not differences:
            status = "XPASS" if gap else "PASS"
        elif (
            gap
            and gap.signature == signature
            and not any(
                forbidden in detail
                for detail in differences
                for forbidden in (
                    "non-loopback network",
                    "blocked host socket",
                    "credential appeared",
                    "config value escaped",
                )
            )
        ):
            status = "XFAIL"
        else:
            status = "DIFF"
        self.failed |= status in {"DIFF", "XPASS"}
        evidence = {
            "id": name,
            "status": status,
            "owner": gap.owner if gap else None,
            "signature": signature if differences else None,
            "expected_signature": gap.signature if gap else None,
            "title": (gap.title or None) if gap else None,
            "differences": differences,
        }
        print(self.row_label + " " + json.dumps(evidence, sort_keys=True), flush=True)
        owner = f"{gap.owner} " if gap else ""
        if status == "XPASS":
            return status, f"{owner}remove the repaired signature"
        if not differences:
            return status, ""
        if status == "DIFF" and gap:
            owner += "signature changed "
        return status, f"{owner}{signature[:12]} " + ", ".join(differences)

    def finish(self) -> int:
        for name in sorted(self.gaps.keys() - self.seen):
            self.failed = True
            print(f"Stale cutover gap {name} ({self.gaps[name].owner})", flush=True)
        return int(self.failed)
