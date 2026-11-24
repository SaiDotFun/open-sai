"""Isolate test homes and prevent connections to external TCP services."""

from __future__ import annotations

import ipaddress
import os
import socket

import pytest


@pytest.fixture(autouse=True)
def block_external_tcp(monkeypatch):
    original = socket.socket.connect

    def connect_local_only(sock, address):
        if isinstance(address, tuple):
            host = address[0]
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                loopback = host.lower() == "localhost"
            if not loopback:
                raise AssertionError(
                    f"External TCP connection forbidden in tests: {host}"
                )
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect_local_only)


@pytest.fixture(autouse=True)
def private_client_home(monkeypatch, tmp_path_factory):
    """Doctor and update must never inspect the developer's MCP registrations."""
    home = tmp_path_factory.mktemp("mcp-home")
    monkeypatch.setenv("HOME", str(home))
    if os.name == "nt":
        monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


@pytest.fixture(autouse=True)
def private_sai_home(monkeypatch, tmp_path_factory):
    """Give every test its own SAI_HOME and fresh log state, so no test
    writes log lines under the real ~/.sai. A test that sets SAI_HOME itself
    overrides this."""
    from sai_cli.core import log

    monkeypatch.setenv("SAI_HOME", str(tmp_path_factory.mktemp("sai-home")))
    for name in ("SAI_LOG", "SAI_LOG_KEEP_DAYS"):
        monkeypatch.delenv(name, raising=False)
    log.reset()
    yield
    log.reset()
