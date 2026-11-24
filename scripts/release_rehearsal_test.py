"""Run with python3 scripts/release-rehearsal.py --self-test."""

import fcntl
import json
import os
import runpy
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

REHEARSAL = runpy.run_path(str(Path(__file__).with_name("release-rehearsal.py")))


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


class ProcessTest(unittest.TestCase):
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
            descendant = (
                "import fcntl, sys, time; file = open(sys.argv[1], 'w'); "
                "fcntl.flock(file, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(30)"
            )
            leader = "import subprocess, sys; subprocess.Popen(sys.argv[1:])"
            command = (
                sys.executable,
                "-c",
                leader,
                sys.executable,
                "-c",
                descendant,
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
            with lock.open("r") as file:
                deadline = time.monotonic() + 2
                while True:
                    try:
                        fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("Descendant still holds the lock")
                        time.sleep(0.01)


class KeeperReplayTest(unittest.TestCase):
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
                        self.assertEqual([row["intentKey"] for row in rows], expected)
