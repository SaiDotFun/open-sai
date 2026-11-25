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
    cli = next(
        step for step in steps if step.startswith("Compare the Rust and Python CLIs")
    )
    assert steps.index(transcript) < steps.index(cli)


def test_cutover_ci_uses_one_release_for_focused_and_full_parity() -> None:
    root = Path(__file__).resolve().parents[2]
    rust = (root / ".github/workflows/ci.yml").read_text().split("\n  rust:\n", 1)[1]
    focused = rust.split("name: Compare focused release parity cases", 1)[1]
    focused, manifest = focused.split(
        "name: Compare the full Rust manifest against the released Python CLI", 1
    )
    assert "env: &release-parity" in focused
    assert "/.parity-python/.venv/bin/sai" in focused
    manifest = manifest.split("\n      - name: ", 1)[0]
    assert "env: *release-parity" in manifest
    assert 'parity.py --reference "$SAI_PARITY_PYTHON" --cutover' in manifest
    assert "--case " not in manifest
