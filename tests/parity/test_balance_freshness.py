"""Check source ages against the LCD on the same loopback Keeper origin."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from overview_scenarios import RISK_BLOCK
from scenarios import ADDRESS, child_env, loopback, seed_home


@pytest.fixture(params=["python", "rust"])
def balance_launcher(request: pytest.FixtureRequest) -> str:
    if request.param == "python":
        return str(Path(sys.executable).with_name("sai"))
    return request.getfixturevalue("cli_pair")[1][0]


def test_balance_snapshot_checks_source_block_age(
    balance_launcher: str, tmp_path: Path
) -> None:
    with loopback("overview-balance-freshness") as endpoints:
        home = tmp_path / "snapshot"
        seed_home(home, endpoints, "session")
        environment = child_env(home, endpoints)
        del environment["SAI_WS_ENDPOINT"]
        result = subprocess.run(
            [balance_launcher, "--json", "account", "balances", ADDRESS],
            cwd=home,
            env=environment,
            capture_output=True,
            check=False,
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert not endpoints.errors
        rows = {
            row["token_info"]["name"]: row
            for row in json.loads(result.stdout)["data"]["userBalances"]
        }
        stale = rows["USDC on Sai"]
        fresh = rows["USDC Unsettled on Sai"]
        unknown = rows["USDC Credits on Sai"]
        assert stale["sourceBlock"] == RISK_BLOCK - 1
        assert 900 <= stale["sourceAgeSeconds"] < 960
        assert stale["stale"] is True
        assert fresh["usd_value"] == 12.125
        assert fresh["sourceBlock"] == RISK_BLOCK
        assert 0 <= fresh["sourceAgeSeconds"] < 60
        assert fresh["stale"] is False
        assert unknown["sourceAgeSeconds"] is unknown["stale"] is None
        assert [event.label for event in endpoints.wire].count(
            "lcd:balance-source"
        ) == 1


@pytest.mark.parametrize("mode", [(), ("--json",)], ids=["text", "json"])
def test_overview_labels_stale_balance(
    balance_launcher: str,
    mode: tuple[str, ...],
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    with loopback("overview-balance-freshness") as endpoints:
        home = tmp_path / "rust"
        seed_home(home, endpoints, "session")
        environment = child_env(home, endpoints)
        del environment["SAI_WS_ENDPOINT"]
        result = subprocess.run(
            [balance_launcher, *mode, "account", "overview", "--as", ADDRESS],
            cwd=home,
            env=environment,
            capture_output=True,
            check=False,
            text=True,
            encoding="utf-8",
            timeout=20,
        )
        assert result.returncode == 0, result.stderr
        assert not endpoints.errors
        if mode:
            rows = json.loads(result.stdout)["balances"]["data"]
            assert rows["balance"]["stale"] is True
            assert rows["unsettled"]["usd"] == 12.125
            assert rows["unsettled"]["stale"] is False
            assert rows["credits"]["stale"] is None
            assert rows["credits"]["sourceAtBasis"] == "fetchedAt"
            return
        funds = result.stdout.split("Sai Funds", 1)[1].split("Open Positions", 1)[0]
        if request.node.callspec.params["balance_launcher"] == "python":
            assert re.search(rf"block {RISK_BLOCK - 1} \(15m \d+s old · stale\)", funds)
        else:
            # The source block is the last balance change, so Rust names the
            # age by that change instead of calling the read stale (#271).
            assert re.search(
                rf"block {RISK_BLOCK - 1} \(unchanged for 15m \d+s\)", funds
            )
            assert "stale" not in funds
        assert re.search(rf"block {RISK_BLOCK} \(0m \d+s old\)", funds)


def test_overview_preserves_balance_stream_error(
    balance_launcher: str, tmp_path: Path
) -> None:
    with (
        loopback("overview-full") as endpoint,
        loopback("ws-wrong-subprotocol") as socket,
    ):
        home = tmp_path / "overview"
        seed_home(home, endpoint, "session")
        environment = {**child_env(home, endpoint), "SAI_WS_ENDPOINT": socket.ws}
        command = ("account", "overview", "--as", ADDRESS)
        for mode in [("--json",), ()]:
            result = subprocess.run(
                [balance_launcher, *mode, *command],
                cwd=home,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=20,
            )
            assert result.returncode == 0, result.stderr
            if mode:
                funds = json.loads(result.stdout)["balances"]
                assert funds["error"]["code"] == "STREAM_ERROR"
                assert "data" not in funds
            else:
                funds = result.stdout.split("Sai Funds", 1)[1].split(
                    "Open Positions", 1
                )[0]
                assert "(STREAM_ERROR)" in funds
                assert "Source (Age)" not in funds
                assert "Unsettled Teller" not in funds
        assert not endpoint.errors
        assert not socket.errors
