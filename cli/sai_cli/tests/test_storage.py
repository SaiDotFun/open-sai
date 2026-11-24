"""Exercise the on-disk trader store with real filesystem and SQLite locks."""

from __future__ import annotations

import os
import select
import subprocess
import sys
from contextlib import closing

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
