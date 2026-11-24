"""Validate the complete GraphQL request before replaying a Keeper response."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from graphql import GraphQLError, Visitor, parse, print_ast, visit
from graphql.language import FieldNode, SelectionSetNode


class OrderFields(Visitor):
    def leave_selection_set(self, node: SelectionSetNode, *_: Any) -> SelectionSetNode:
        selections = node.selections
        if all(isinstance(selection, FieldNode) for selection in selections):
            selections = tuple(sorted(selections, key=print_ast))
        return SelectionSetNode(selections=selections)


def canonical_document(document: str) -> str:
    return print_ast(visit(parse(document, no_location=True), OrderFields()))


DOCUMENTS = {
    canonical_document(document): label
    for label, document in json.loads(
        Path(__file__).with_name("keeper-requests.json").read_text(encoding="utf-8")
    ).items()
}


@dataclass(frozen=True)
class WireRequest:
    label: str
    document: str | None = None
    operation_name: str | None = None
    variables: Any = None


def graphql_request(payload: dict[str, Any], *, websocket: bool = False) -> WireRequest:
    try:
        document = canonical_document(payload["query"])
        label = DOCUMENTS[document]
        # The recorded documents are anonymous and the envelopes omit operationName.
        if label.startswith("ws:") != websocket or "operationName" in payload:
            raise ValueError
        return WireRequest(label, document, variables=payload["variables"])
    except (GraphQLError, AttributeError, KeyError, TypeError, ValueError):
        transport = "WebSocket" if websocket else "Keeper HTTP"
        raise ValueError(f"{transport} GraphQL request differs from fixture") from None


def same_variables(actual: Any, expected: dict[str, Any]) -> bool:
    # JSON integers and floats compare equal in Python. GraphQL integer inputs
    # and other typed values must keep their wire representations.
    return json.dumps(actual, sort_keys=True) == json.dumps(expected, sort_keys=True)
