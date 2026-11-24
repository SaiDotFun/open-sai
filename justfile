# Wrapper that makes calling `just` show the command list
[private]
list:
    @just --list

install:
    uv sync --all-groups

test:
    uv run pytest

# Compare the Python CLI with SAI_PARITY_BIN, or run the Python self-check
parity:
    @uv run python tests/parity/parity.py

check:
    uv run ruff check .
    uv run ruff format --check .
    uv run ty check cli/sai_cli

# Build the wheel and sdist and check that both ship the real skill directory
build-check:
    uv run python scripts/check_dist.py

# Tag main as sai-cli/vVERSION and push it; dry run unless given --run
release VERSION *FLAGS:
    uv run python scripts/release.py {{VERSION}} {{FLAGS}}

# Copy the sai skill from jiyuu into skills/sai; dry run unless given --run
sync-skill *FLAGS:
    uv run python scripts/sync_skill.py {{FLAGS}}
