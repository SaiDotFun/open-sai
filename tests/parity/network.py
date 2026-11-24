"""Run the parity driver and its subprocesses in an isolated Linux network."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

NETNS = Path("/proc/self/ns/net")
ORIGINAL_NETNS = "_SAI_PARITY_ORIGINAL_NETNS"


def system_tool(name: str) -> str:
    path = shutil.which(name, path="/usr/sbin:/usr/bin:/sbin:/bin")
    if path is None:
        raise ValueError(f"Loopback isolation requires the Linux command {name}")
    return path


def enter_loopback() -> None:
    if sys.platform != "linux":
        raise ValueError(
            "Parity requires a Linux network namespace for loopback isolation"
        )
    current = os.readlink(NETNS)
    original = os.environ.pop(ORIGINAL_NETNS, None)
    if original is None:
        unshare = system_tool("unshare")
        env = {**os.environ, ORIGINAL_NETNS: current}
        os.execve(unshare, [unshare, "-Urn", "--", sys.executable, *sys.argv], env)
    if current == original or [name for _, name in socket.if_nameindex()] != ["lo"]:
        raise ValueError("Parity could not establish a loopback-only network namespace")

    result = subprocess.run(
        [system_tool("ip"), "link", "set", "lo", "up"],
        capture_output=True,
        check=False,
        timeout=5,
    )
    if result.returncode:
        raise ValueError("Parity could not enable loopback in its network namespace")
    unroutable_packets()


def restricted(argv: list[str]) -> list[str]:
    # Children inherit the namespace but cannot administer it or gain
    # privileges through a setuid program.
    return [
        system_tool("setpriv"),
        "--no-new-privs",
        "--bounding-set=-all",
        "--",
        *argv,
    ]


def unroutable_packets() -> tuple[int, int]:
    """Count unroutable IPv4 and IPv6 sends, including TCP connect attempts."""
    try:
        columns, values = [
            line.split()
            for line in Path("/proc/net/snmp").read_text().splitlines()
            if line.startswith("Ip:")
        ]
        ipv4 = int(values[columns.index("OutNoRoutes")])
        ipv6_path = Path("/proc/net/snmp6")
        ipv6 = (
            dict(line.split() for line in ipv6_path.read_text().splitlines())
            if ipv6_path.exists()
            else {}
        )
        return ipv4, int(ipv6["Ip6OutNoRoutes"]) if ipv6 else 0
    except (KeyError, OSError, ValueError):
        raise ValueError("Parity could not read network namespace counters") from None
