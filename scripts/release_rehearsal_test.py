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
from contextlib import suppress
from pathlib import Path
from typing import Any
from unittest.mock import patch

REHEARSAL = runpy.run_path(str(Path(__file__).with_name("release-rehearsal.py")))
LOCK_DESCENDANT = (
    "import fcntl, sys, time; file = open(sys.argv[1], 'w'); "
    "fcntl.flock(file, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
)


class RetryBlockTest(unittest.TestCase):
    def test_candidate_requires_structured_code_and_original_key(self) -> None:
        key = "original-intent"
        for code, reported_key, exit_code, accepted in (
            ("RECENT_OPEN_PENDING", key, 1, True),
            ("RECENT_OPEN_PENDING", "other-intent", 1, False),
            ("INVALID_REQUEST", key, 1, False),
            ("RECENT_OPEN_PENDING", key, 0, False),
        ):
            with self.subTest(
                code=code, reported_key=reported_key, exit_code=exit_code
            ):
                payload = {
                    "error": {
                        "code": code,
                        "intentKey": reported_key,
                        "message": "wording can change",
                    }
                }
                result = subprocess.CompletedProcess(
                    [], exit_code, json.dumps(payload), ""
                )
                self.assertEqual(
                    REHEARSAL["is_retry_block"](result, key, legacy=False), accepted
                )

    def test_legacy_message_is_accepted_only_for_the_original_key_and_legacy_cli(
        self,
    ) -> None:
        key = "original-intent"
        stderr = (
            f"Error: Reconcile uncertain intent {key} before submitting another trade"
        )
        result = subprocess.CompletedProcess([], 1, "", stderr)
        self.assertTrue(REHEARSAL["is_retry_block"](result, key, legacy=True))
        self.assertFalse(
            REHEARSAL["is_retry_block"](result, "other-intent", legacy=True)
        )
        self.assertFalse(REHEARSAL["is_retry_block"](result, key, legacy=False))


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
    def test_strict_requires_a_namespace_before_installing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            bin_dir = root / "tools"
            bin_dir.mkdir()
            for name in ("uv", "claude", "codex"):
                (bin_dir / name).symlink_to(sys.executable)
            wheel = root / "sai_cli-0.1.1-py3-none-any.whl"
            wheel.write_bytes(b"unused wheel")
            script = Path(__file__).with_name("release-rehearsal.py").resolve()
            result = subprocess.run(
                [sys.executable, str(script), "--strict", str(wheel)],
                cwd=root,
                env={"HOME": temp, "TMPDIR": temp, "PATH": str(bin_dir)},
                timeout=5,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn(
                "--strict requires a loopback-only network namespace", result.stderr
            )
            self.assertEqual(result.stdout, "")
            self.assertCountEqual(root.iterdir(), [bin_dir, wheel])

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

    def assert_interruption_cleans_up(
        self, sig: signal.Signals, teardown_hook: str = ""
    ) -> None:
        script = Path(__file__).with_name("release-rehearsal.py").resolve()
        parent = """
import runpy, signal, sys, tempfile
from pathlib import Path
entry = runpy.run_path(sys.argv[1])
hook = sys.argv[3]

def interrupt_at_teardown(frame, event, arg):
    if event == "call" and frame.f_code.co_name == hook:
        sys.setprofile(None)
        signal.raise_signal(signal.SIGTERM)

try:
    with entry["cancel_on_signal"](), entry["cleanup_stack"]() as cleanup:
        home = cleanup.enter_context(tempfile.TemporaryDirectory(dir=sys.argv[2]))
        with entry["owned_process"](tuple(sys.argv[4:]), Path(home), {"HOME": home}) as child:
            if entry["ReplyReader"](child.stdout).line("descendant") != "locked":
                sys.exit(2)
            print(child.pid, home, flush=True)
            if hook:
                sys.setprofile(interrupt_at_teardown)
            else:
                child.communicate()
except entry["RehearsalInterrupted"] as exc:
    sys.exit(128 + exc.signum)
"""
        with (
            tempfile.TemporaryDirectory() as temp,
            REHEARSAL["owned_process"](
                (
                    sys.executable,
                    "-c",
                    parent,
                    script,
                    temp,
                    teardown_hook,
                    sys.executable,
                    "-c",
                    "import subprocess, sys; subprocess.run(sys.argv[1:])",
                    sys.executable,
                    "-c",
                    LOCK_DESCENDANT,
                    Path(temp) / "descendant.lock",
                ),
                Path(temp),
                {},
            ) as process,
        ):
            lock = Path(temp) / "descendant.lock"
            group, home_name = (
                REHEARSAL["ReplyReader"](process.stdout)
                .line("parent ready")
                .split(" ", 1)
            )
            home = Path(home_name)
            try:
                if not teardown_hook:
                    self.assertTrue(home.is_dir())
                    process.send_signal(sig)
                self.assertEqual(process.wait(timeout=5), 128 + sig)
                self.assertFalse(home.exists())
                self.assert_group_released(lock)
            except BaseException:
                with suppress(ProcessLookupError):
                    os.killpg(int(group), signal.SIGKILL)
                raise

    def test_parent_signals_stop_the_group_and_remove_the_home(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self.assert_interruption_cleans_up(sig)

    def test_sigterm_during_teardown_stops_the_group_and_removes_the_home(self) -> None:
        for hook in ("close_stack", "stop_process_group"):
            with self.subTest(hook=hook):
                self.assert_interruption_cleans_up(signal.SIGTERM, hook)

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
    def setUp(self) -> None:
        temp = self.enterContext(tempfile.TemporaryDirectory())
        wheel = Path(temp) / "sai_cli-0.1.1-py3-none-any.whl"
        wheel.write_bytes(b"unused wheel")
        self.replay = REHEARSAL["Replay"](wheel, "0.1.1")
        self.key = "20261124T080000000Z_ABCDEFGHJKMNPQRS"
        self.replay.submitted.append(self.key)

    def query(self, base: str, text: str, **variables: str | None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{base}/query",
            data=json.dumps({"query": text, "variables": variables}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response)["data"]

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
        addr, key = self.replay.addr, self.key
        query = (
            "query($user:String!, $key:String) {"
            " userIntents(where:{user:$user, intentKey:$key}) { intentKey } }"
        )
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            for text, user, intent, expected in (
                (query, addr.upper(), key, [key]),
                (query, "0x" + "b" * 40, key, []),
                (query, addr, "wrong-key", []),
                (query, addr, None, []),
                (query.replace("intentKey:$key", 'intentKey:"wrong"'), addr, key, []),
            ):
                with self.subTest(user=user, key=intent, query=text):
                    rows = self.query(base, text, user=user, key=intent)["userIntents"]
                    self.assertEqual(rows, [{"intentKey": key} for key in expected])

    def test_checks_filters_independently_for_each_intent_alias(self) -> None:
        query = """
query($user: String!, $key: String, $otherKey: String, $otherUser: String!) {
  userIntents(where: {user: $user, intentKey: $otherKey}) { intentKey }
  matched: userIntents(where: {user: $user, intentKey: $key}, limit: 50) {
    id: intentKey
  }
  reordered: userIntents(limit: 50, where: {intentKey: $key, user: $user}) { intentKey }
  wrongUser: userIntents(where: {user: $otherUser, intentKey: $key}) { intentKey }
  missingKey: userIntents(where: {user: $user}) { intentKey }
}
"""
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            data = self.query(
                base,
                query,
                user=self.replay.addr,
                key=self.key,
                otherKey="wrong-key",
                otherUser="0x" + "b" * 40,
            )
        self.assertEqual(
            data,
            {
                "userIntents": [],
                "matched": [{"id": self.key}],
                "reordered": [{"intentKey": self.key}],
                "wrongUser": [],
                "missingKey": [],
            },
        )
