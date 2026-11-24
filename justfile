# Wrapper that makes calling `just` show the command list
[private]
list:
    @just --list

install:
    uv sync --all-groups

test:
    uv run pytest

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check cli/sai_cli

# Build the Rust workspace in debug mode
rust-build:
    cargo build --locked --workspace

# Run the Rust workspace tests
rust-test:
    cargo test --locked --workspace

# Build the wheel and sdist and check that both ship the real skill directory
build-check:
    uv run python scripts/check_dist.py

# Tag main as sai-cli/vVERSION and push it; dry run unless given --run
release VERSION *FLAGS:
    uv run python scripts/release.py {{VERSION}} {{FLAGS}}

# Copy the sai skill from jiyuu into skills/sai; dry run unless given --run
sync-skill *FLAGS:
    uv run python scripts/sync_skill.py {{FLAGS}}

# Generate the Markdown and JSON log event references
log-events:
    uv run python -m sai_cli.core.log_events
