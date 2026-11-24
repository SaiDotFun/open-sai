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
from urllib.error import HTTPError

SCRIPT = Path(__file__).with_name("release-rehearsal.py").resolve()
REHEARSAL = runpy.run_path(str(SCRIPT))
RUN_WITH_RELEASE_API = (
    "import runpy, sys; script, api = sys.argv[1:3]; "
    "sys.argv = [script, *sys.argv[3:]]; "
    "sys.exit(runpy.run_path(script)['main'](api_base=api))"
)
LOCK_DESCENDANT = (
    "import fcntl, os, sys, time; file = open(sys.argv[1], 'w'); "
    "fcntl.flock(file, fcntl.LOCK_EX); print('locked', os.getpid(), flush=True); time.sleep(30)"
)


def published_release(version: str) -> dict[str, Any]:
    return {
        "tag_name": f"sai-cli/v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {"name": f"sai_cli-{version}-py3-none-any.whl", "state": "uploaded"},
            {"name": "SHA256SUMS", "state": "uploaded"},
        ],
    }


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
                self.assertEqual(
                    REHEARSAL["is_retry_block"](result, key, legacy=True), accepted
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


class BaselineTest(unittest.TestCase):
    def test_lost_acknowledgement_requires_the_original_intent_in_both_formats(
        self,
    ) -> None:
        key = "original-intent"
        legacy = subprocess.CompletedProcess(
            [],
            1,
            "",
            f"Error: Outcome unknown for intent {key}; reconcile before another trade",
        )
        structured = subprocess.CompletedProcess(
            [],
            5,
            json.dumps({"error": {"code": "OUTCOME_UNKNOWN", "intentKey": key}}),
            "",
        )
        for result in (legacy, structured):
            with self.subTest(exit_code=result.returncode):
                self.assertTrue(REHEARSAL["is_lost_acknowledgement"](result, key))
                self.assertFalse(
                    REHEARSAL["is_lost_acknowledgement"](result, "other-intent")
                )

    def test_default_uses_highest_published_sai_cli_version_across_pages(self) -> None:
        keeper_handler = REHEARSAL["KeeperHandler"]

        class Releases(keeper_handler):
            def do_GET(self) -> None:
                if self.path.endswith("?per_page=100&page=1"):
                    body = json.dumps(
                        [
                            {"tag_name": "rust/v5.0.0"},
                            {"tag_name": "sai-cli/v0.1.9"},
                            {"tag_name": "sai-cli/v9.0.0", "draft": True},
                            {"tag_name": "sai-cli/v8.0.0", "prerelease": True},
                        ]
                    ).encode()
                    self.send_response(200)
                    self.send_header("Link", '<ignored>; rel="next"')
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path.endswith("?per_page=100&page=2"):
                    self.reply([{"tag_name": "sai-cli/v0.1.10"}])
                else:
                    self.send_error(404)

        with REHEARSAL["keeper_server"](Releases) as base:
            self.assertEqual(REHEARSAL["resolve_baseline"](base, base), "0.1.10")

    def test_missing_or_unavailable_published_baseline_fails(self) -> None:
        keeper_handler = REHEARSAL["KeeperHandler"]

        class NoReleases(keeper_handler):
            def do_GET(self) -> None:
                if self.path.startswith("/repos/"):
                    self.reply([{"tag_name": "rust/v5.0.0"}])
                else:
                    self.send_error(404)

        with REHEARSAL["keeper_server"](NoReleases) as base:
            with self.assertRaisesRegex(REHEARSAL["RehearsalError"], "No.*published"):
                REHEARSAL["resolve_baseline"](base, base)
            with self.assertRaisesRegex(
                REHEARSAL["RehearsalError"], "Could not resolve.*--from"
            ):
                REHEARSAL["resolve_baseline"](f"{base}/invalid", base)

    def test_candidate_has_to_be_newer_than_the_explicit_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            wheel = Path(temp) / "sai_cli-0.1.9-py3-none-any.whl"
            wheel.touch()
            script = Path(__file__).with_name("release-rehearsal.py").resolve()
            result = subprocess.run(
                [sys.executable, str(script), "--from", "0.1.10", str(wheel)],
                timeout=5,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Candidate sai 0.1.9 must be newer than baseline sai 0.1.10", result.stderr
        )

    def test_explicit_baseline_requires_the_exact_stable_release_and_assets(
        self,
    ) -> None:
        keeper_handler = REHEARSAL["KeeperHandler"]

        class Releases(keeper_handler):
            def do_GET(self) -> None:
                if self.path.endswith("/releases/tags/sai-cli%2Fv0.1.0"):
                    self.reply(published_release("0.1.0"))
                elif self.path.endswith("/releases/tags/sai-cli%2Fv0.1.1"):
                    self.reply({**published_release("0.1.1"), "prerelease": True})
                elif self.path.endswith("/releases/tags/sai-cli%2Fv0.1.2"):
                    release = published_release("0.1.2")
                    release["assets"].pop()
                    self.reply(release)
                else:
                    self.send_error(404)

        with (
            REHEARSAL["keeper_server"](Releases) as base,
            tempfile.TemporaryDirectory() as temp,
        ):
            REHEARSAL["verify_published_baseline"]("0.1.0", base)
            wheel = Path(temp) / "sai_cli-0.1.3-py3-none-any.whl"
            wheel.touch()
            for version, detail in (
                ("0.0.999", "release not found on GitHub"),
                ("0.1.1", "expected a stable release"),
                ("0.1.2", "missing release assets SHA256SUMS"),
            ):
                with self.subTest(version=version):
                    result = subprocess.run(
                        [
                            sys.executable,
                            "-c",
                            RUN_WITH_RELEASE_API,
                            str(SCRIPT),
                            base,
                            "--from",
                            version,
                            str(wheel),
                        ],
                        env={"HOME": temp, "TMPDIR": temp, "PATH": ""},
                        timeout=5,
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn(
                        f"Could not verify published sai-cli release {version}: {detail}",
                        result.stderr,
                    )
                    self.assertCountEqual(Path(temp).iterdir(), [wheel])

    def test_truncated_release_response_reports_baseline_lookup_failure(self) -> None:
        keeper_handler = REHEARSAL["KeeperHandler"]

        class TruncatedRelease(keeper_handler):
            def do_GET(self) -> None:
                self.send_response(200)
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                self.wfile.write(b"10\r\n[\r\n")
                self.close_connection = True

        with (
            REHEARSAL["keeper_server"](TruncatedRelease) as base,
            tempfile.TemporaryDirectory() as temp,
        ):
            wheel = Path(temp) / "sai_cli-0.1.2-py3-none-any.whl"
            wheel.touch()
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    RUN_WITH_RELEASE_API,
                    str(SCRIPT),
                    base,
                    str(wheel),
                ],
                timeout=5,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "Could not resolve the latest published sai-cli release", result.stderr
        )
        self.assertIn("incomplete or invalid GitHub HTTP response", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class SessionTest(unittest.TestCase):
    def test_changed_or_missing_session_file_fails_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tool = REHEARSAL["Install"](Path(temp), prepared=True)
            tool.session_file.parent.mkdir(parents=True)
            original = b"original encrypted session"
            tool.session_file.write_bytes(original)
            snapshot = tool.session_file.read_bytes()
            token = REHEARSAL["SYNTHETIC_TOKEN"]
            expected = {"endpoint": "http://127.0.0.1:12345/query", "token": token}

            for contents in (b"changed encrypted session", None):
                with self.subTest(contents=contents):
                    if contents is None:
                        tool.session_file.unlink()
                    else:
                        tool.session_file.write_bytes(contents)
                    with self.assertRaisesRegex(
                        REHEARSAL["RehearsalError"], "The stored session file changed"
                    ) as error:
                        tool.verify_session(snapshot, expected)
                    self.assertNotIn(token, str(error.exception))


class NetworkTest(unittest.TestCase):
    def test_strict_requires_a_namespace_before_installing(self) -> None:
        keeper_handler = REHEARSAL["KeeperHandler"]

        class Release(keeper_handler):
            def do_GET(self) -> None:
                self.reply(published_release("0.1.0"))

        with (
            tempfile.TemporaryDirectory() as temp,
            REHEARSAL["keeper_server"](Release) as base,
        ):
            root = Path(temp)
            bin_dir = root / "tools"
            bin_dir.mkdir()
            for name in ("uv", "claude", "codex"):
                (bin_dir / name).symlink_to(sys.executable)
            wheel = root / "sai_cli-0.1.1-py3-none-any.whl"
            wheel.write_bytes(b"unused wheel")
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    RUN_WITH_RELEASE_API,
                    str(SCRIPT),
                    base,
                    "--strict",
                    "--from",
                    "0.1.0",
                    str(wheel),
                ],
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
    def test_delayed_command_reads_all_input_across_cancellation_polls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            tool = REHEARSAL["Install"](Path(temp), prepared=True)
            tool.home.mkdir()
            payload = "x" * 2_000_000
            result = tool.run(
                "Read input after a delay",
                sys.executable,
                "-c",
                "import sys, time; time.sleep(0.25); print(len(sys.stdin.read()))",
                input_text=payload,
                timeout=5,
                env={"HOME": str(tool.home)},
            )
            self.assertEqual(result.stdout.strip(), str(len(payload)))

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
        self, sig: signal.Signals, teardown_hook: str = "", *, detached: bool = False
    ) -> None:
        script = Path(__file__).with_name("release-rehearsal.py").resolve()
        launcher = (
            "import subprocess, sys; "
            f"subprocess.run(sys.argv[1:], start_new_session={detached!r})"
        )
        parent = """
import os, runpy, signal, sys, tempfile
from pathlib import Path
entry = runpy.run_path(sys.argv[1])
hook = sys.argv[3]
signum = int(sys.argv[4])

def interrupt_at_teardown(frame, event, arg):
    if event != "call":
        return
    name = frame.f_code.co_name
    if name == "__exit__":
        gen = getattr(frame.f_locals.get("self"), "gen", None)
        if gen is not None:
            name = gen.gi_code.co_name + ".__exit__"
    if name == hook:
        sys.setprofile(None)
        signal.raise_signal(signum)

def verify_child_stopped(child):
    if child.returncode is None:
        raise RuntimeError("Child still running at the start of scratch cleanup")

try:
    with entry["cancel_on_signal"](), entry["cleanup_stack"]() as cleanup:
        home = cleanup.enter_context(tempfile.TemporaryDirectory(dir=sys.argv[2]))
        with entry["reap_detached_children"]():
            process_context = entry["owned_process"](tuple(sys.argv[5:]), Path(home), {"HOME": home})
            with process_context as child:
                cleanup.callback(verify_child_stopped, child)
                status, descendant = entry["ReplyReader"](child.stdout).line("descendant").split()
                if status != "locked":
                    sys.exit(2)
                print(os.getpgid(child.pid), descendant, home, flush=True)
                if hook:
                    sys.setprofile(interrupt_at_teardown)
                else:
                    entry["communicate"](child, timeout=30)
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
                    str(sig.value),
                    sys.executable,
                    "-c",
                    launcher,
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
            group, descendant, home_name = (
                REHEARSAL["ReplyReader"](process.stdout)
                .line("parent ready")
                .split(" ", 2)
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
                if detached:
                    with suppress(ProcessLookupError):
                        os.kill(int(descendant), signal.SIGKILL)
                with suppress(ProcessLookupError):
                    os.killpg(int(group), signal.SIGKILL)
                raise

    def test_parent_signals_stop_the_group_and_remove_the_home(self) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self.assert_interruption_cleans_up(sig)

    def test_sigterm_during_teardown_stops_the_group_and_removes_the_home(self) -> None:
        for hook in ("cleanup_stack.__exit__", "stop_process_group"):
            with self.subTest(hook=hook):
                self.assert_interruption_cleans_up(signal.SIGTERM, hook)

    def test_signals_at_process_exit_stop_the_child_before_scratch_cleanup(
        self,
    ) -> None:
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                self.assert_interruption_cleans_up(sig, "owned_process.__exit__")

    def test_sigterm_during_handler_restoration_reports_interruption(self) -> None:
        script = Path(__file__).with_name("release-rehearsal.py").resolve()
        program = """
import runpy, signal, sys
entry = runpy.run_path(sys.argv[1])

def interrupt_at_restore(frame, event, arg):
    if (event == "call" and frame.f_code is signal.signal.__code__
            and frame.f_locals["signalnum"] == signal.SIGTERM):
        sys.setprofile(None)
        signal.raise_signal(signal.SIGTERM)

def work():
    with entry["cancel_on_signal"]():
        sys.setprofile(interrupt_at_restore)
    return 0

sys.exit(entry["report_result"](work))
"""
        result = subprocess.run(
            [sys.executable, "-c", program, script],
            env={},
            timeout=5,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 128 + signal.SIGTERM)
        self.assertIn("interrupted by SIGTERM", result.stderr)

    @unittest.skipUnless(sys.platform == "linux", "Linux subreaper coverage")
    def test_sigterm_stops_a_helper_in_a_separate_session(self) -> None:
        self.assert_interruption_cleans_up(signal.SIGTERM, detached=True)

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
                    REHEARSAL["ReplyReader"](child.stdout).line("lock").split()[0],
                    "locked",
                )
                child.communicate(timeout=0.1)
            self.assert_group_released(lock)

    def test_group_leader_stays_owned_after_communicate_reaps_the_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            command = (sys.executable, "-c", "print('done')")
            with REHEARSAL["owned_process"](
                command, Path(temp), {"HOME": temp}
            ) as process:
                leader = os.getpgid(process.pid)
                self.assertEqual(process.communicate(timeout=5)[0], b"done\n")
                self.assertEqual(process.returncode, 0)
                self.assertNotEqual(leader, process.pid)
                os.kill(leader, 0)
            with self.assertRaises(ChildProcessError):
                os.waitpid(leader, os.WNOHANG)


class KeeperReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        temp = self.enterContext(tempfile.TemporaryDirectory())
        wheel = Path(temp) / "sai_cli-0.1.1-py3-none-any.whl"
        wheel.write_bytes(b"unused wheel")
        self.replay = REHEARSAL["Replay"](wheel, "0.1.1")
        self.key = "20261124T080000000Z_ABCDEFGHJKMNPQRS"
        self.replay.submitted.append(self.key)

    def query(
        self, base: str, text: str, **variables: str | int | None
    ) -> dict[str, Any]:
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

    def test_projects_trade_history_with_variable_pagination_and_legacy_limit(
        self,
    ) -> None:
        self.replay.history = [
            {"id": number, "intentKey": self.key, "trade": {"trader": self.replay.addr}}
            for number in (3, 2, 1)
        ]
        paginated = """
query($trader:String!, $limit:Int!, $offset:Int!) {
  perp {
    tradeHistory(where:{trader:$trader}, order_by:sequence, order_desc:true,
                 limit:$limit, offset:$offset) { id trade { trader } }
  }
}
"""
        legacy = (
            "query($trader:String!) { perp { tradeHistory(where:{trader:$trader},"
            " limit:50, order_desc:true) { id } } }"
        )
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            for offset, ids in ((0, (3, 2)), (2, (1,)), (4, ())):
                with self.subTest(offset=offset):
                    data = self.query(
                        base, paginated, trader=self.replay.addr, limit=2, offset=offset
                    )
                    self.assertEqual(
                        data["perp"]["tradeHistory"],
                        [
                            {"id": number, "trade": {"trader": self.replay.addr}}
                            for number in ids
                        ],
                    )
            data = self.query(base, legacy, trader=self.replay.addr)
            self.assertEqual(
                data["perp"]["tradeHistory"], [{"id": n} for n in (3, 2, 1)]
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

    def test_filters_each_history_selection_before_paginating(self) -> None:
        other = "0x" + "b" * 40
        self.replay.history = [
            {"id": number, "trade": {"trader": trader}}
            for number, trader in (
                (3, other),
                (2, self.replay.addr),
                (1, self.replay.nibi_addr),
            )
        ]
        query = """
query($trader:String!, $other:String!, $limit:Int!, $offset:Int!) {
  perp {
    matching: tradeHistory(where:{trader:$trader}, limit:$limit, offset:$offset) { id }
    wrong: tradeHistory(where:{trader:$other}, limit:$limit, offset:$offset) { id }
    missing: tradeHistory(limit:$limit, offset:$offset) { id }
  }
}
"""
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            data = self.query(
                base, query, trader=self.replay.addr, other=other, limit=1, offset=1
            )
        self.assertEqual(
            data, {"perp": {"matching": [{"id": 1}], "wrong": [], "missing": []}}
        )

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

    def test_rejects_an_undeclared_filter_variable_even_if_supplied(self) -> None:
        query = """
query($user: String!, $key: String) {
  userIntents(where: {user: $user, intentKey: $key}) { intentKey }
  other: userIntents(where: {user: $user, intentKey: $undeclared}) { intentKey }
}
"""
        with (
            REHEARSAL["keeper_server"](self.replay.handler()) as base,
            self.assertRaises(HTTPError) as error,
        ):
            self.query(
                base, query, user=self.replay.addr, key=self.key, undeclared=self.key
            )
        with error.exception as response:
            self.assertEqual(response.code, 400)

    def test_rejects_invalid_intent_filter_variable_declarations(self) -> None:
        query = """
query($user: String!, $key: String, $other: DECLARATION) {
  userIntents(where: {user: $user, intentKey: $key}) { intentKey }
  invalid: userIntents(where: {FILTER}) { intentKey }
}
"""
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            cases = (
                ("Int", "user: $user, intentKey: $other", self.key),
                ("[String]", "user: $other, intentKey: $key", self.replay.addr),
                ("String = 7", "user: $user, intentKey: $other", self.key),
                ("String = $key", "user: $user, intentKey: $other", self.key),
                ("String! = null", "user: $user, intentKey: $other", self.key),
            )
            for declaration, filters, value in cases:
                with self.subTest(declaration=declaration):
                    with self.assertRaises(HTTPError) as error:
                        self.query(
                            base,
                            query.replace("DECLARATION", declaration).replace(
                                "FILTER", filters
                            ),
                            user=self.replay.addr,
                            key=self.key,
                            other=value,
                        )
                    with error.exception as response:
                        self.assertEqual(response.code, 400)

    def test_user_filter_requires_a_non_null_declaration_or_default(self) -> None:
        query = """
query($account: DECLARATION, $key: String) {
  matched: userIntents(where: {user: $account, intentKey: $key}) { intentKey }
}
"""
        addr = self.replay.addr
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            for declaration in ("String", "String = null"):
                with self.subTest(declaration=declaration):
                    with self.assertRaises(HTTPError) as error:
                        self.query(
                            base,
                            query.replace("DECLARATION", declaration),
                            account=addr,
                            key=self.key,
                        )
                    with error.exception as response:
                        self.assertEqual(response.code, 400)
            defaulted = query.replace("DECLARATION", f"String = {json.dumps(addr)}")
            self.assertEqual(
                self.query(base, defaulted, key=self.key),
                {"matched": [{"intentKey": self.key}]},
            )

    def test_uses_intent_filter_defaults_only_for_omitted_variables(self) -> None:
        query = (
            f"query($user: String! = {json.dumps(self.replay.addr)},"
            f" $key: String = {json.dumps(self.key)}) {{"
            " matched: userIntents(where: {user: $user, intentKey: $key}) { intentKey } }"
        )
        with REHEARSAL["keeper_server"](self.replay.handler()) as base:
            self.assertEqual(
                self.query(base, query), {"matched": [{"intentKey": self.key}]}
            )
            for key in (None, "wrong-key"):
                with self.subTest(key=key):
                    self.assertEqual(self.query(base, query, key=key), {"matched": []})
