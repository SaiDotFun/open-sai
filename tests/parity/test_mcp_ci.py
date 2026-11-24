"""Keep both the native binary and the C8 Python pin in the Rust CI job."""

from pathlib import Path


def test_rust_ci_runs_pinned_mcp_transcript() -> None:
    root = Path(__file__).resolve().parents[2]
    rust_job = (
        (root / ".github/workflows/ci.yml").read_text().split("\n  rust:\n", 1)[1]
    )
    steps = rust_job.split("\n      - name: ")
    reference = next(step for step in steps if step.startswith("Check out the C8 MCP"))
    assert "ref: ea09f0d417a53a71079bb8c5e426005fa29051a4" in reference
    assert "path: .parity-mcp-python" in reference
    transcript = next(
        step
        for step in steps
        if "python tests/parity/parity.py --case 'mcp-*-*'" in step
    )
    assert "SAI_PARITY_BIN: ${{ github.workspace }}/target/debug/sai" in transcript
    assert ".parity-mcp-python/.venv/bin/sai" in transcript
