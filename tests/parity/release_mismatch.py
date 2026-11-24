"""Describe release differences without logging observed values."""

import hashlib
import json
from collections.abc import Iterable
from typing import Any


def difference_signature(differences: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(differences)).encode()).hexdigest()


class ReleaseMismatch(AssertionError):
    def __init__(self, differences: list[str]) -> None:
        self.differences = differences
        self.signature = difference_signature(differences)
        super().__init__(
            f"release mismatch {self.signature}\n" + "\n".join(differences)
        )


class ExpectedReleaseMismatch(ReleaseMismatch):
    """The observed difference matches a reviewed release exception."""


def differing_paths(reference: Any, candidate: Any, path: str) -> list[str]:
    if type(reference) is not type(candidate):
        return [f"{path}: {type(reference).__name__} -> {type(candidate).__name__}"]
    if isinstance(reference, dict):
        differences = [
            f"{path}.{key}: missing"
            for key in sorted(reference.keys() - candidate.keys())
        ]
        differences.extend(
            f"{path}.{key}: extra"
            for key in sorted(candidate.keys() - reference.keys())
        )
        for key in sorted(reference.keys() & candidate.keys()):
            differences.extend(
                differing_paths(reference[key], candidate[key], f"{path}.{key}")
            )
        return differences
    if isinstance(reference, (list, tuple)):
        differences = []
        if len(reference) != len(candidate):
            differences.append(f"{path}: length {len(reference)} -> {len(candidate)}")
        for index, (left, right) in enumerate(zip(reference, candidate)):
            differences.extend(differing_paths(left, right, f"{path}[{index}]"))
        return differences
    if reference == candidate:
        return []
    if isinstance(reference, (int, bool)) and path.endswith(".exit"):
        return [f"{path}: {reference} -> {candidate}"]
    if isinstance(reference, bytes) and path.endswith(".stdout"):
        try:
            left, right = json.loads(reference), json.loads(candidate)
        except (ValueError, UnicodeError):
            pass
        else:
            return differing_paths(left, right, path) or [f"{path}: encoding"]
    return [path]


def assert_release_equal(reference: Any, candidate: Any) -> None:
    if differences := differing_paths(reference, candidate, "parity"):
        raise ReleaseMismatch(differences)
