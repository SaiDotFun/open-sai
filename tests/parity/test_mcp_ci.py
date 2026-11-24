"""Keep both the native binary and the C8 Python pin in the Rust CI job."""

from pathlib import Path


def test_rust_ci_runs_pinned_mcp_transcript() -> None:
    root = Path(__file__).resolve().parents[2]
    rust_job = (
        (root / ".github/workflows/ci.yml").read_text().split("\n  rust:\n", 1)[1]
    )
    steps = rust_job.split("\n      - name: ")
    reference = next(step for step in steps if step.startswith("Check out the C8 MCP"))
    assert "ref: 6a2e3af1edab943f204d4110db97024c9d7b8c63" in reference
    assert "path: .parity-mcp-python" in reference
    transcript = next(
        step
        for step in steps
        if "python tests/parity/parity.py --case 'mcp-*-*'" in step
    )
    assert "SAI_PARITY_BIN: ${{ github.workspace }}/target/debug/sai" in transcript
    assert ".parity-mcp-python/.venv/bin/sai" in transcript
    cli = next(
        step for step in steps if step.startswith("Compare the Rust and Python CLIs")
    )
    assert steps.index(transcript) < steps.index(cli)
