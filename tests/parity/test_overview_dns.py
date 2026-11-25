"""Exercise LCD cancellation while the system resolver is blocked."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from network import restricted
from scenarios import child_env, loopback, seed_home

pytestmark = [
    pytest.mark.parity_namespace,
    pytest.mark.skipif(sys.platform != "linux", reason="Requires Linux LD_PRELOAD"),
]

# Only the synthetic LCD hostname stalls. The real CLI and Keeper replay
# continue to use their normal HTTP and WebSocket stacks.
STALL_DNS = r"""
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <netdb.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int getaddrinfo(const char *host, const char *service,
                const struct addrinfo *hints, struct addrinfo **result) {
    if (host && strcmp(host, "overview-dns.invalid") == 0) {
        int fd = open(getenv("SAI_PARITY_DNS_READY"), O_CREAT | O_WRONLY, 0600);
        if (fd >= 0) close(fd);
        sleep(8);
        return EAI_AGAIN;
    }
    int (*resolve)(const char *, const char *, const struct addrinfo *, struct addrinfo **);
    resolve = dlsym(RTLD_NEXT, "getaddrinfo");
    return resolve(host, service, hints, result);
}
"""


@pytest.mark.parametrize("interrupt", [False, True], ids=["deadline", "sigint"])
def test_overview_exits_while_lcd_dns_is_stalled(
    tmp_path: Path, cli_pair: tuple[tuple[str, ...], tuple[str, ...]], interrupt: bool
) -> None:
    _, candidate = cli_pair
    shim = tmp_path / "stall-dns.so"
    subprocess.run(
        ["cc", "-shared", "-fPIC", "-x", "c", "-o", str(shim), "-", "-ldl"],
        input=STALL_DNS,
        text=True,
        capture_output=True,
        check=True,
    )
    ready = tmp_path / "dns-ready"

    with loopback("overview-full") as endpoints:
        home = tmp_path / "home"
        seed_home(home, endpoints, "empty")
        args = ["--json", "account", "overview", "--as", endpoints.expand("{addr}")]
        env = {
            **child_env(home, endpoints),
            "SAI_LCD_ENDPOINT": "https://overview-dns.invalid",
            "LD_PRELOAD": str(shim),
            "SAI_PARITY_DNS_READY": str(ready),
        }
        with subprocess.Popen(
            restricted([*candidate, *args]),
            cwd=home,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        ) as process:
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and process.poll() is None:
                    assert time.monotonic() < deadline, "LCD DNS did not start"
                    time.sleep(0.01)
                assert ready.is_file(), "LCD DNS did not start"
                if interrupt:
                    os.killpg(process.pid, signal.SIGINT)
                try:
                    output, stderr = process.communicate(
                        timeout=1.5 if interrupt else 5
                    )
                except subprocess.TimeoutExpired:
                    pytest.fail(
                        "Overview waited for the stalled DNS resolver to finish"
                    )
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=3)

        assert not stderr.strip()
        assert not endpoints.errors
        assert process.returncode == (1 if interrupt else 0)
        result = json.loads(output)
        if interrupt:
            assert result["error"]["message"] == "Aborted"
        else:
            assert result["positions"]["membership"]["status"] == "unavailable"
            assert len(result["positions"]["data"]["items"]) == 2
            assert result["queuedMarketOrders"]["data"][0]["sequence"] == 400
