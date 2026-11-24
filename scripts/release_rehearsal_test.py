"""Run with python3 scripts/release-rehearsal.py --self-test."""

import fcntl
import json
import os
import runpy
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

REHEARSAL = runpy.run_path(str(Path(__file__).with_name("release-rehearsal.py")))
LOCK_DESCENDANT = (
    "import fcntl, sys, time; file = open(sys.argv[1], 'w'); "
    "fcntl.flock(file, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
)


class LauncherTest(unittest.TestCase):
    def test_env_launcher_finds_its_runtime_in_a_fresh_home(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "launchers"
            bin_dir.mkdir()
            runtime_dir = root / "outside-system-path"
            runtime_dir.mkdir()
            (runtime_dir / "fixture-python").symlink_to(sys.executable)
            for tool in ("uv", "claude"):
                (bin_dir / tool).symlink_to(sys.executable)
            app_dir = root / "real app"
            app_dir.mkdir()
            launcher = app_dir / "codex"
            launcher.write_text(
                "#!/usr/bin/env -S fixture-python -I\n"
                "from pathlib import Path\n"
                "print(Path(__file__).with_name('sibling').read_text())\n"
                "print(Path.home())\n"
            )
            launcher.chmod(0o700)
            (app_dir / "sibling").write_text("runtime started")
            (bin_dir / "codex").symlink_to(launcher)
            path = os.pathsep.join((str(bin_dir), str(runtime_dir)))
            with patch.dict(os.environ, PATH=path):
                tool = REHEARSAL["Install"](root)
                result = tool.run("Run client fixture", tool.sai.with_name("codex"))
            self.assertEqual(
                result.stdout.splitlines(), ["runtime started", str(tool.home)]
            )


class DiagnosticsTest(unittest.TestCase):
    def test_child_exit_reports_only_step_and_code_unless_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "tools"
            bin_dir.mkdir()
            for name in ("uv", "claude", "codex"):
                (bin_dir / name).symlink_to(sys.executable)
            diagnostic = "child fixture output"
            command = (
                sys.executable,
                "-c",
                f"import sys; print({diagnostic!r}); print({diagnostic!r}, file=sys.stderr); sys.exit(17)",
            )
            with patch.dict(os.environ, PATH=str(bin_dir)):
                for verbose in (False, True):
                    scratch = root / str(verbose)
                    scratch.mkdir()
                    log = scratch / "child-output.log"
                    tool = REHEARSAL["Install"](scratch, log if verbose else None)
                    with self.assertRaisesRegex(
                        REHEARSAL["RehearsalError"], r"\AFailing fixture exited 17\Z"
                    ):
                        tool.run("Failing fixture", *command)
                    if verbose:
                        self.assertEqual(log.read_text().count(diagnostic), 2)
                    else:
                        self.assertFalse(log.exists())


class NetworkTest(unittest.TestCase):
    def test_namespace_isolates_direct_connections_from_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as temp, socket.socket() as host:
            root = Path(temp)
            env = {"HOME": temp, "PATH": os.defpath}
            prefix = REHEARSAL["namespace_prefix"](root, env)
            if prefix is None:
                self.skipTest("loopback-only network namespace unavailable")
            host.bind(("127.0.0.1", 0))
            host.listen()
            script = """
import errno, socket, sys
for address, expected in (
    (("127.0.0.1", int(sys.argv[1])), errno.ECONNREFUSED),
    (("192.0.2.1", 9), errno.ENETUNREACH),
):
    try:
        with socket.create_connection(address, timeout=2):
            sys.exit(1)
    except OSError as exc:
        if exc.errno != expected:
            sys.exit(1)
"""
            command = (
                *prefix,
                sys.executable,
                "-I",
                "-c",
                script,
                str(host.getsockname()[1]),
            )
            with REHEARSAL["owned_process"](command, root, env) as process:
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0)

    def test_direct_loopback_works_and_external_http_uses_the_dead_proxy(self) -> None:
        class Handler(REHEARSAL["KeeperHandler"]):
            def do_GET(self) -> None:
                self.reply({"fixture": True})

        with (
            REHEARSAL["keeper_server"](Handler) as base,
            socket.socket(socket.AF_INET, socket.SOCK_STREAM) as proxy,
        ):
            proxy.bind(("127.0.0.1", 0))
            env = REHEARSAL["loopback_env"](base, proxy.getsockname()[1])
            self.assertEqual(env["SAI_ENDPOINT"], f"{base}/query")
            self.assertEqual(env["SAI_LCD_ENDPOINT"], base)
            self.assertEqual(
                env["SAI_WS_ENDPOINT"], f"ws://{base.removeprefix('http://')}/query"
            )
            self.assertEqual(env["UV_OFFLINE"], "1")
            subprocess.run(
                [sys.executable, "-I", "-c", REHEARSAL["CHECK_NETWORK"], base],
                env=env,
                check=True,
                timeout=10,
                capture_output=True,
            )


class ProcessTest(unittest.TestCase):
    def assert_group_released(self, lock: Path) -> None:
        with lock.open("r") as file:
            deadline = time.monotonic() + 2
            while True:
                try:
                    fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        self.fail("Descendant still holds the lock")
                    time.sleep(0.01)

    def test_parent_signals_stop_the_group_and_remove_the_home(self) -> None:
        script = Path(__file__).with_name("release-rehearsal.py").resolve()
        parent = """
import runpy, sys, tempfile
from pathlib import Path
entry = runpy.run_path(sys.argv[1])
try:
    with entry["cancel_on_signal"](), tempfile.TemporaryDirectory(dir=sys.argv[2]) as home:
        with entry["owned_process"](tuple(sys.argv[3:]), Path(home), {"HOME": home}) as child:
            if entry["ReplyReader"](child.stdout).line("descendant") != "locked":
                sys.exit(2)
            print(home, flush=True)
            child.communicate()
except entry["RehearsalInterrupted"] as exc:
    sys.exit(128 + exc.signum)
"""
        with tempfile.TemporaryDirectory() as temp:
            lock = Path(temp) / "descendant.lock"
            for sig in (signal.SIGTERM, signal.SIGINT):
                with (
                    self.subTest(signal=sig.name),
                    REHEARSAL["owned_process"](
                        (
                            sys.executable,
                            "-c",
                            parent,
                            script,
                            temp,
                            sys.executable,
                            "-c",
                            LOCK_DESCENDANT,
                            lock,
                        ),
                        Path(temp),
                        {},
                    ) as process,
                ):
                    home = Path(
                        REHEARSAL["ReplyReader"](process.stdout).line("parent ready")
                    )
                    self.assertTrue(home.is_dir())
                    process.send_signal(sig)
                    self.assertEqual(process.wait(timeout=5), 128 + sig)
                    self.assertFalse(home.exists())
                    self.assert_group_released(lock)

    def test_partial_reply_times_out(self) -> None:
        read, write = os.pipe()
        with os.fdopen(read, "rb") as source, os.fdopen(write, "wb") as writer:
            reader = REHEARSAL["ReplyReader"](source)
            writer.write(b"first\nsecond\npartial")
            writer.flush()
            self.assertEqual(reader.line("first"), "first")
            self.assertEqual(reader.line("second"), "second")
            with self.assertRaisesRegex(REHEARSAL["RehearsalError"], "complete reply"):
                reader.line("partial", timeout=0.05)

    def test_timeout_kills_a_descendant_after_the_leader_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lock = root / "descendant.lock"
            leader = "import subprocess, sys; subprocess.Popen(sys.argv[1:])"
            command = (
                sys.executable,
                "-c",
                leader,
                sys.executable,
                "-c",
                LOCK_DESCENDANT,
                lock,
            )
            with (
                self.assertRaises(subprocess.TimeoutExpired),
                REHEARSAL["owned_process"](command, root, {"HOME": temp}) as child,
            ):
                self.assertEqual(
                    REHEARSAL["ReplyReader"](child.stdout).line("lock"), "locked"
                )
                child.communicate(timeout=0.1)
            self.assert_group_released(lock)


class KeeperReplayTest(unittest.TestCase):
    def test_projects_nested_and_aliased_fields(self) -> None:
        fields = REHEARSAL["selection_set"](
            "query TradeStatus($key: String) {\n"
            " rows: userIntents(where:{intentKey:$key}) { intentKey # errorCode\n"
            " trade { owner: trader } } }"
        )
        payload = {
            "userIntents": [
                {
                    "intentKey": "the-key",
                    "errorCode": "market_order_outcome_not_indexed",
                    "trade": {"trader": "trader-address", "id": 123},
                }
            ]
        }
        self.assertEqual(
            REHEARSAL["project_fields"](payload, fields),
            {"rows": [{"intentKey": "the-key", "trade": {"owner": "trader-address"}}]},
        )

    def test_requires_the_original_intent_key_and_account(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "sai_cli-0.1.1-py3-none-any.whl"
            wheel.write_bytes(b"unused wheel")
            replay = REHEARSAL["Replay"](wheel, "0.1.1")
            key = "20261124T080000000Z_ABCDEFGHJKMNPQRS"
            replay.submitted.append(key)
            query = (
                "query($user:String!, $key:String) {"
                " userIntents(where:{user:$user, intentKey:$key}) { intentKey } }"
            )
            with REHEARSAL["keeper_server"](replay.handler()) as base:
                for text, user, intent, expected in (
                    (query, replay.addr.upper(), key, [key]),
                    (query, "0x" + "b" * 40, key, []),
                    (query, replay.addr, "wrong-key", []),
                    (query, replay.addr, None, []),
                    (
                        query.replace("intentKey:$key", 'intentKey:"wrong"'),
                        replay.addr,
                        key,
                        [],
                    ),
                ):
                    with self.subTest(user=user, key=intent, query=text):
                        request = urllib.request.Request(
                            f"{base}/query",
                            data=json.dumps(
                                {
                                    "query": text,
                                    "variables": {"user": user, "key": intent},
                                }
                            ).encode(),
                            headers={"Content-Type": "application/json"},
                        )
                        with urllib.request.urlopen(request, timeout=2) as response:
                            rows = json.load(response)["data"]["userIntents"]
                        self.assertEqual(rows, [{"intentKey": key} for key in expected])
