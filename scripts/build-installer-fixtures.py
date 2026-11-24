"""Build the pinned loopback fixtures for the installer rehearsals."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from check_release import ROOT, pyproject_version

BASELINE = "8557a7e13dceab0accc369191182c8e01423c91b"
MCP = "df224a3477c1151636cd2ac90fece7d38e4b4c07"
UPDATE = "d5e0437d2e664a70063a2246fb90cae9d9656aa2"
DIST = "4bd968eb956c154f0a0ab53ccc288ceb78dd8713"


def fixture_versions() -> tuple[str, str]:
    major, minor, patch = map(int, pyproject_version().split("."))
    # These versions exist only on the replay server. Use a nonzero native
    # patch to exercise release selection independently of the cutover tag.
    return f"{major}.{minor}.{patch + 1}", f"{major}.{minor + 1}.{patch + 1}"


def run(*command: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True)


def checkout_files(ref: str, destination: Path, *paths: str) -> None:
    for name in run(
        "git", "ls-tree", "-r", "--name-only", ref, "--", *paths
    ).splitlines():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(
            subprocess.check_output(["git", "show", f"{ref}:{name}"], cwd=ROOT)
        )


def edit(path: Path, old: str, new: str) -> None:
    source = path.read_text()
    if source.count(old) != 1:
        raise SystemExit(
            f"could not apply the installer fixture overlay to {path.name}"
        )
    path.write_text(source.replace(old, new))


MUTATION = """
    if option_env!("SAI_INSTALLER_MUTANT").is_some()
        && args.get(1).is_some_and(|arg| arg == "mcp")
        && args.get(2).is_some_and(|arg| arg == "serve")
        && (Path::new(&args[0]).ends_with(".local/bin/sai")
            || env::var_os("SAI_NATIVE_UV_PREFIX")
                .is_some_and(|prefix| Path::new(&prefix).ends_with("sai-cli")))
    {
        eprintln!("installed MCP startup mutation");
        return process::ExitCode::FAILURE;
    }
"""

MCP_LOG_ADAPTER = """
pub use privacy::origin as endpoint_origin;

pub fn enabled(env: &Environment) -> bool {
    settings::log_keep_days(env).is_some()
}

impl<'a> EventContext<'a> {
    pub fn new(writer: Option<&'a LogWriter>, surface: Surface, command: Option<&'a str>) -> Self {
        Self { writer, surface, command }
    }

    pub fn with_tool(self, _name: &str, _call_id: &str) -> Self {
        self
    }
}
"""


def prepare_rust(source: Path, version: str) -> None:
    for directory in ("sai", "sai-core", ".cargo"):
        if (ROOT / directory).is_dir():
            shutil.copytree(ROOT / directory, source / directory)
    for file in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml"):
        shutil.copyfile(ROOT / file, source / file)

    main = source / "sai/src/main.rs"
    cargo = source / "sai/Cargo.toml"
    workspace = source / "Cargo.toml"
    metadata = workspace.read_text()
    workspace.write_text(
        re.sub(
            r'^version = ".*"$',
            f'version = "{version}"',
            metadata,
            count=1,
            flags=re.MULTILINE,
        )
    )
    if not (source / "sai/src/mcp.rs").is_file():
        checkout_files(
            MCP, source, "sai/src/mcp.rs", "sai/src/mcp", "sai/src/reads/output.rs"
        )
        edit(main, "mod reads;", "mod reads;\nmod mcp;")
        edit(source / "sai/src/reads.rs", "mod output;", "pub(crate) mod output;")
        # Adapt the older logging interface. The MCP transport and server are
        # built directly from the pinned PR.
        log = source / "sai-core/src/log.rs"
        with log.open("a") as output:
            output.write(MCP_LOG_ADAPTER)
        registration = source / "sai/src/mcp_registration.rs"
        edit(
            registration,
            "pub(crate) enum McpCommand {",
            "pub(crate) enum McpCommand {\n    #[command(flatten)]\n    Serve(crate::mcp::McpCommand),",
        )
        edit(
            registration,
            "            Some(McpCommand::List) => {",
            '            Some(McpCommand::Serve(_)) => unreachable!("dispatched in main"),\n'
            "            Some(McpCommand::List) => {",
        )
        edit(
            main,
            "            Command::Mcp(args) =>",
            "            Command::Mcp(mcp_registration::McpArgs {\n"
            "                command: Some(mcp_registration::McpCommand::Serve(command)),\n"
            "            }) => command.run(flags, env, None),\n"
            "            Command::Mcp(args) =>",
        )
        edit(
            cargo,
            "[dependencies]\n",
            '[dependencies]\nrmcp = { version = "3.11", default-features = false, features = ["local", "server", "transport-io"] }\n'
            "uuid.workspace = true\n",
        )
    if not (source / "sai/src/update.rs").is_file():
        checkout_files(UPDATE, source, "sai/src/update.rs", "sai/src/update")
        edit(main, "mod reads;", "mod reads;\nmod update;")
        edit(
            main,
            "    Doctor(doctor::DoctorArgs),",
            "    Doctor(doctor::DoctorArgs),\n    Update(update::UpdateCommand),",
        )
        edit(
            main,
            "            Command::Doctor(args) =>",
            "            Command::Update(command) => command.run(&flags, env, json, events),\n"
            "            Command::Doctor(args) =>",
        )
        edit(
            workspace,
            "[workspace.dependencies]\n",
            "[workspace.dependencies]\n"
            'reqwest = { version = "0.12", default-features = false, features = ["rustls-tls-native-roots"] }\n'
            'semver = "1"\ntar = "0.4"\n',
        )
        edit(
            cargo,
            "[dependencies]\n",
            "[dependencies]\n"
            'percent-encoding = "2"\nflate2.workspace = true\nreqwest.workspace = true\n'
            "semver.workspace = true\nsha2.workspace = true\ntar.workspace = true\nurl.workspace = true\n",
        )
        with cargo.open("rb") as handle:
            features = tomllib.load(handle)["dependencies"]["tokio"]["features"]
        required = sorted(set(features) | {"io-util", "process", "signal"})
        edit(
            cargo,
            f"features = {json.dumps(features)}",
            f"features = {json.dumps(required)}",
        )
    edit(
        main,
        "    let args: Vec<_> = env::args_os().collect();",
        "    let args: Vec<_> = env::args_os().collect();" + MUTATION,
    )


def build_wheels(output: Path, bridge: str, native: str) -> None:
    baseline = output / "pre-bridge"
    old = output / "baseline-source"
    run("git", "worktree", "add", "--detach", str(old), BASELINE)
    run("uv", "build", "--wheel", str(old), "--out-dir", str(baseline))
    shutil.copyfile(old / "scripts/install.py", baseline / "install.py")
    (baseline / "commit").write_text(BASELINE + "\n")
    run(
        sys.executable,
        "scripts/build-bridge.py",
        "--version",
        bridge,
        "--out-dir",
        str(output / "bridge"),
    )
    if (ROOT / "shim").is_dir():
        shutil.copytree(ROOT / "shim", output / "shim-source/shim")
    else:
        checkout_files(DIST, output / "shim-source", "shim")
    checkout_files(
        DIST, output / "shim-source", "tests/parity/native_release_source.py"
    )
    shim = output / "shim-source/shim"
    shutil.copyfile(
        output / "shim-source/tests/parity/native_release_source.py",
        shim / "src/sai_cli/_release_source.py",
    )
    project = shim / "pyproject.toml"
    project.write_text(
        re.sub(
            r'^version = ".*"$',
            f'version = "{native}"',
            project.read_text(),
            flags=re.MULTILINE,
        )
    )
    run("uv", "build", "--wheel", str(shim), "--out-dir", str(output / "shim"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--rust-only", action="store_true")
    mode.add_argument("--wheels-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    bridge, native = fixture_versions()
    output.mkdir(parents=True, exist_ok=True)
    if not args.wheels_only:
        build_rust(output, native)
    if not args.rust_only:
        build_wheels(output, bridge, native)


def build_rust(output: Path, version: str) -> None:
    source = output / "rust-source"
    source.mkdir()
    prepare_rust(source, version)
    cargo = ["cargo", "build", "--bin", "sai"]
    subprocess.run(cargo, cwd=source, check=True)
    binary = Path(os.environ.get("CARGO_TARGET_DIR", source / "target")) / "debug/sai"
    shutil.copy2(binary, output / "sai")
    subprocess.run(["strip", "--strip-debug", output / "sai"], check=True)
    subprocess.run(
        cargo, cwd=source, env={**os.environ, "SAI_INSTALLER_MUTANT": "1"}, check=True
    )
    shutil.copy2(binary, output / "sai-mutant")
    subprocess.run(["strip", "--strip-debug", output / "sai-mutant"], check=True)


if __name__ == "__main__":
    main()
