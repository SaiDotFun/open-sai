import json
from pathlib import Path

import pytest
from manifest_gaps import ManifestGaps
from parity import Case, Observation, mcp_evidence, mcp_tools, report_mcp_tools
from release_mismatch import value_fingerprint


def test_mcp_reports_fingerprint_unexpected_names_and_metadata(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def transcript(*tools: dict[str, str]) -> Observation:
        return Observation(
            0,
            [
                {"id": 1, "result": {"protocolVersion": "2025-11-25"}},
                {"id": 2, "result": {"tools": list(tools)}},
            ],
            (),
            {},
        )

    public_name = "logs_intent"
    unexpected_names = ("synthetic-secret-extra", "synthetic-secret-reference")
    metadata = "synthetic-secret-description"
    reference = transcript({"name": public_name}, {"name": unexpected_names[1]})
    candidate = transcript(
        {"name": public_name, "description": metadata},
        {"name": unexpected_names[0], "description": metadata},
    )
    evidence: dict[str, list[str]] = {}
    case = Case("mcp-report-test", ["mcp", "serve"], mcp_profile="initialize")
    mcp_evidence(case, reference, candidate, evidence)

    expected, actual = mcp_tools(reference.output), mcp_tools(candidate.output)
    statuses = {
        name: "EXTRA"
        if name not in expected
        else "MISSING"
        if name not in actual
        else "DIFF"
        for name in expected.keys() | actual.keys()
    }
    registry = tmp_path / "gaps.toml"
    registry.write_text("[gap]\n")
    gaps = ManifestGaps(registry)
    report_mcp_tools(statuses, evidence, gaps)
    output = capsys.readouterr().out
    rows = {
        entry["id"]: entry
        for line in output.splitlines()
        if line.startswith("CUTOVER_ROW ")
        for entry in [json.loads(line.removeprefix("CUTOVER_ROW "))]
    }
    labels = [
        f"unexpected-sha256:{value_fingerprint(name)}" for name in unexpected_names
    ]
    assert rows.keys() == {f"mcp:{name}" for name in [public_name, *labels]}
    assert all(entry["status"] == "DIFF" for entry in rows.values())
    assert gaps.finish() == 1
    assert all(f"`{name}`" in output for name in [public_name, *labels])
    assert all(marker not in output for marker in (*unexpected_names, metadata))

    report_mcp_tools(statuses, evidence, None)
    legacy = capsys.readouterr().out
    assert all(f"`{name}`" in legacy for name in [public_name, *labels])
    assert all(marker not in legacy for marker in (*unexpected_names, metadata))
