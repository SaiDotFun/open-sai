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


def fingerprint(path: str, *values: Any) -> str:
    def canonical(value: Any) -> tuple[str, Any]:
        kind = type(value).__qualname__
        if isinstance(value, dict):
            return kind, [
                (canonical(key), canonical(value[key])) for key in sorted(value)
            ]
        if isinstance(value, (tuple, list)):
            return kind, [canonical(item) for item in value]
        if isinstance(value, bytes):
            return kind, value.hex()
        return kind, value

    encoded = json.dumps(
        [canonical(value) for value in values], separators=(",", ":"), default=str
    ).encode()
    return f"{path} [sha256:{hashlib.sha256(encoded).hexdigest()}]"


def differing_paths(reference: Any, candidate: Any, path: str) -> list[str]:
    if type(reference) is not type(candidate):
        return [
            fingerprint(
                f"{path}: {type(reference).__name__} -> {type(candidate).__name__}",
                reference,
                candidate,
            )
        ]
    if isinstance(reference, dict):
        differences = [
            fingerprint(f"{path}.{key}: missing", reference[key])
            for key in sorted(reference.keys() - candidate.keys())
        ]
        differences.extend(
            fingerprint(f"{path}.{key}: extra", candidate[key])
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
            shared = min(len(reference), len(candidate))
            differences.append(
                fingerprint(
                    f"{path}: length {len(reference)} -> {len(candidate)}",
                    reference[shared:],
                    candidate[shared:],
                )
            )
        for index, (left, right) in enumerate(zip(reference, candidate)):
            differences.extend(differing_paths(left, right, f"{path}[{index}]"))
        return differences
    if reference == candidate:
        return []
    if isinstance(reference, bytes) and path.endswith(".stdout"):
        try:
            left, right = json.loads(reference), json.loads(candidate)
        except (ValueError, UnicodeError):
            pass
        else:
            return differing_paths(left, right, path) + [
                fingerprint(f"{path}: bytes", reference, candidate)
            ]
    return [fingerprint(path, reference, candidate)]


def assert_release_equal(reference: Any, candidate: Any) -> None:
    if differences := differing_paths(reference, candidate, "parity"):
        raise ReleaseMismatch(differences)
