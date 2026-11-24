"""Exercise the on-disk trader store with real filesystem and SQLite locks."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from contextlib import ExitStack, closing
from pathlib import Path

import pytest
from sai_cli.core import storage


@pytest.mark.skipif(os.name == "nt", reason="POSIX SQLite locks and pipes")
def test_second_ledger_open_preserves_write_lock() -> None:
    program = """
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect(sys.argv[1], timeout=0)) as connection:
    for _ in range(2):
        try:
            connection.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as exc:
            if exc.sqlite_errorcode != sqlite3.SQLITE_BUSY:
                raise
            print('waiting', flush=True)
        else:
            raise AssertionError('acquired the write lock before the holder committed')
        assert sys.stdin.readline() == 'retry\\n'
    connection.execute('PRAGMA busy_timeout = 5000')
    with connection:
        connection.execute('BEGIN IMMEDIATE')
    print('acquired')
"""
    with closing(storage.db()) as holder:
        holder.execute("BEGIN IMMEDIATE")
        with subprocess.Popen(
            [sys.executable, "-c", program, str(storage.home() / "intents.sqlite3")],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as writer:
            try:
                assert writer.stdin is not None and writer.stdout is not None
                assert select.select([writer.stdout], [], [], 10)[0]
                assert writer.stdout.readline().strip() == "waiting"

                with closing(storage.db()) as second:
                    second.execute("SELECT count(*) FROM intents").fetchone()

                writer.stdin.write("retry\n")
                writer.stdin.flush()
                assert select.select([writer.stdout], [], [], 10)[0]
                assert writer.stdout.readline().strip() == "waiting"

                holder.commit()
                stdout, stderr = writer.communicate("retry\n", timeout=10)
                assert writer.returncode == 0, stderr
                assert stdout.strip() == "acquired"
            finally:
                if writer.poll() is None:
                    writer.kill()


FIRST_WRITE = """
import os
import sys
from sai_cli.core import log, settings, storage, updater

entry = sys.argv[1]
os.umask(int(sys.argv[2], 8))
print('ready', flush=True)
assert sys.stdin.readline() == 'start\\n'
if entry == 'storage':
    storage.db().close()
elif entry == 'logging':
    log.event('update.scheduled')
    assert log.flush(5)
    assert list(log.directory().glob('*.jsonl'))
elif entry == 'config':
    settings.edit_file(settings.config_path(os.environ), lambda _: '[update]\\nauto = false\\n')
elif entry == 'update-state':
    assert updater.save_state({'result': 'test'})
elif entry == 'update-cache':
    updater.private_cache()
"""

ENTRY_DIRS = {
    "storage": ("trader",),
    "logging": ("logs",),
    "config": (),
    "update-state": (),
    "update-cache": ("cache", "cache/wheels"),
}


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes")
@pytest.mark.parametrize("entry", ENTRY_DIRS)
def test_first_write_creates_private_home(entry: str, tmp_path: Path, monkeypatch):
    home = tmp_path / "missing-parent" / "sai"
    monkeypatch.setenv("SAI_HOME", str(home))
    result = subprocess.run(
        [sys.executable, "-c", FIRST_WRITE, entry, "022"],
        input="start\n",
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    for directory in (home, *(home / name for name in ENTRY_DIRS[entry])):
        assert directory.stat().st_mode & 0o777 == 0o700, directory


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes and pipes")
def test_concurrent_first_writes_share_private_home(tmp_path: Path, monkeypatch):
    home = tmp_path / "sai"
    monkeypatch.setenv("SAI_HOME", str(home))
    with ExitStack() as stack:
        workers = []
        for entry in ENTRY_DIRS:
            worker = stack.enter_context(
                subprocess.Popen(
                    [sys.executable, "-c", FIRST_WRITE, entry, "000"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            stack.callback(worker.kill)
            workers.append(worker)
        for worker in workers:
            assert worker.stdout is not None
            assert select.select([worker.stdout], [], [], 10)[0]
            assert worker.stdout.readline().strip() == "ready"
        assert not home.exists()
        for worker in workers:
            assert worker.stdin is not None
            worker.stdin.write("start\n")
            worker.stdin.flush()
        for worker in workers:
            _stdout, stderr = worker.communicate(timeout=10)
            assert worker.returncode == 0, stderr

    for directory in (
        home,
        *(home / name for dirs in ENTRY_DIRS.values() for name in dirs),
    ):
        assert directory.stat().st_mode & 0o777 == 0o700, directory
