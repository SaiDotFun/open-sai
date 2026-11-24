"""Read-only update support: tags, latest fetch, install kind, cache, and
`sai update --check` against loopback GitHub stand-ins."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self

import pytest
from click.testing import CliRunner
from sai_cli.core import update
from sai_cli.sai_cli import (
    main,
    package_version,
    print_update_report,
    update_report,
)

API_PATH = f"/repos/{update.REPO}/releases"
WEB_PATH = f"/{update.REPO}/releases/latest"

# path -> (status, headers, body)
Routes = dict[str, tuple[int, dict[str, str], bytes]]


class Stub:
    """A loopback HTTP server answering fixed routes, recording requests."""

    def __init__(
        self,
        routes: Routes,
        delay: float = 0.0,
        drip: float = 0.0,
        drip_paths: frozenset[str] = frozenset(),
    ) -> None:
        """DRIP sends each response on DRIP_PATHS one byte at a time, DRIP
        seconds apart, so no single read waits long enough to time out."""
        self.routes = routes
        self.requests: list[tuple[str, str]] = []
        self.queries: list[str] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def answer(self, body: bool) -> None:
                # Only requests about the repository count. A thread another
                # test left running can reach this server when the kernel
                # hands its old port to this one.
                if update.REPO in self.path:
                    stub.requests.append((self.command, self.path.partition("?")[0]))
                    stub.queries.append(self.path.partition("?")[2])
                time.sleep(delay)
                path = self.path.partition("?")[0]
                route = stub.routes.get(self.path) or stub.routes.get(path)
                status, headers, data = route or (404, {}, b"")
                if path in drip_paths:
                    self.drip(status, headers, data if body else b"")
                    return
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if body:
                    self.wfile.write(data)

            def drip(self, status: int, headers: dict[str, str], data: bytes) -> None:
                lines = [f"HTTP/1.1 {status} X", f"Content-Length: {len(data)}"]
                lines += [f"{name}: {value}" for name, value in headers.items()]
                raw = ("\r\n".join(lines) + "\r\n\r\n").encode() + data
                try:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(drip)
                except OSError:
                    pass
                self.close_connection = True

            def do_GET(self) -> None:
                self.answer(body=True)

            def do_HEAD(self) -> None:
                self.answer(body=False)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()


def listing(
    *tags: str, more: bool = False, **flags: bool
) -> tuple[int, dict[str, str], bytes]:
    """One page of the API's release list holding TAGS, newest first. MORE
    adds the Link header GitHub sends when another page follows."""
    items = [{"tag_name": tag, "draft": False, "prerelease": False} for tag in tags]
    for item in items:
        item.update(flags)
    headers = {"Content-Type": "application/json"}
    if more:
        headers["Link"] = '<https://api.github.com/next>; rel="next"'
    return 200, headers, json.dumps(items).encode()


def redirect(location: str) -> tuple[int, dict[str, str], bytes]:
    return 302, {"Location": location}, b""


def fetch(stub: Stub, total_timeout: float = update.TOTAL_TIMEOUT) -> str:
    return update.fetch_latest(
        api_base=stub.url, web_base=stub.url, total_timeout=total_timeout
    ).tag


@pytest.fixture(autouse=True)
def sai_home(tmp_path, monkeypatch) -> Path:
    home = tmp_path / "sai-home"
    monkeypatch.setenv("SAI_HOME", str(home))
    return home


# Tags


def test_parse_tag_accepts_sai_cli_release_tags_only():
    assert update.parse_tag("sai-cli/v0.1.0") == (0, 1, 0)
    assert update.parse_tag("sai-cli/v10.20.30") == (10, 20, 30)
    rejected = (
        "v0.1.0",
        "0.1.0",
        "sai-cli/0.1.0",
        "sai-cli/v0.1",
        "sai-cli/v0.2.0-rc.1",
        "sai-cli/v1.0.0+build",
        "sai-rs/v0.1.0",
        "sai-cli-rs/v0.1.0",
        "x/sai-cli/v0.1.0",
        "sai-cli/v0.01.0",
        "sai-cli/v00.1.0",
        "sai-cli/v0.1.01",
        "release",
        "",
    )
    for tag in rejected:
        assert update.parse_tag(tag) is None, tag


def test_tag_and_version_round_trip():
    assert update.tag_for("0.2.0") == "sai-cli/v0.2.0"
    assert update.tag_version("sai-cli/v0.2.0") == "0.2.0"
    assert update.parse_version("0.2.0") == (0, 2, 0)
    assert update.parse_version("sai-cli/v0.2.0") is None
    assert update.parse_version("0.02.0") is None


def test_is_newer_compares_numerically_and_ignores_unparseable():
    assert update.is_newer("sai-cli/v0.10.0", "0.9.9")
    assert update.is_newer("sai-cli/v1.0.0", "0.99.99")
    assert not update.is_newer("sai-cli/v0.1.0", "0.1.0")
    assert not update.is_newer("sai-cli/v0.1.0", "0.2.0")
    assert not update.is_newer("sai-cli/v0.3.0-rc.1", "0.2.0")
    assert not update.is_newer("sai-cli/v0.3.0", "0.2.0.dev1")
    assert not update.is_newer("v0.3.0", "0.2.0")


# Latest release


def test_latest_tag_takes_the_highest_sai_cli_release():
    releases = [
        {"tag_name": "sai-rs/v9.0.0"},
        {"tag_name": "sai-cli/v0.3.0-rc.1", "prerelease": True},
        {"tag_name": "sai-cli/v0.9.0", "prerelease": True},
        {"tag_name": "sai-cli/v0.8.0", "draft": True},
        {"tag_name": "sai-cli/v0.2.1"},
        {"tag_name": "v5.0.0"},
        {"tag_name": "sai-cli/v0.10.0"},
        {"tag_name": "sai-cli/v0.2.0"},
        {"tag_name": None},
        "junk",
    ]
    assert update.latest_tag(releases) == "sai-cli/v0.10.0"
    assert update.latest_tag(releases[:4]) is None
    assert update.latest_tag([]) is None


def test_fetch_picks_the_sai_cli_release_among_mixed_prefixes(capsys):
    tags = ("sai-rs/v3.0.0", "sai-cli/v0.3.0-rc.1", "sai-cli/v0.2.0", "v7.0.0")
    with Stub({API_PATH: listing(*tags)}) as stub:
        found = update.fetch_latest(api_base=stub.url, web_base=stub.url)
    assert found.tag == "sai-cli/v0.2.0"
    assert found.version == "0.2.0"
    base = f"{stub.url}/{update.REPO}/releases/download/sai-cli/v0.2.0"
    assert found.wheel_url == f"{base}/sai_cli-0.2.0-py3-none-any.whl"
    assert found.sums_url == f"{base}/SHA256SUMS"
    assert stub.requests == [("GET", API_PATH)]
    assert stub.queries == [f"per_page={update.PAGE_SIZE}&page=1"]
    assert capsys.readouterr() == ("", "")


def test_release_urls_default_to_github():
    found = update.release_for("sai-cli/v1.2.3")
    assert found.wheel_url == (
        "https://github.com/SaiDotFun/open-sai/releases/download/"
        "sai-cli/v1.2.3/sai_cli-1.2.3-py3-none-any.whl"
    )
    assert found.sums_url.endswith("/sai-cli/v1.2.3/SHA256SUMS")


def paged(*pages: tuple[str, ...]) -> Routes:
    """PAGES of release tags, each but the last announcing a next page."""
    routes: Routes = {}
    for number, tags in enumerate(pages, start=1):
        query = f"per_page={update.PAGE_SIZE}&page={number}"
        routes[f"{API_PATH}?{query}"] = listing(*tags, more=number < len(pages))
    return routes


def test_fetch_reads_every_page_and_takes_the_highest_version():
    """A maintenance release published later sits on an earlier page than
    the higher version; the higher version still wins."""
    routes = paged(
        ("sai-rs/v9.0.0", "sai-cli/v1.9.1"),
        ("sai-cli/v2.0.0", "sai-cli/v1.9.0"),
        ("sai-cli/v0.6.0",),
    )
    with Stub(routes) as stub:
        assert fetch(stub) == "sai-cli/v2.0.0"
    assert stub.queries == [
        f"per_page={update.PAGE_SIZE}&page={number}" for number in (1, 2, 3)
    ]


def test_fetch_finds_a_release_behind_pages_of_other_components():
    routes = paged(("sai-rs/v3.0.0",), ("sai-rs/v2.0.0",), ("sai-cli/v0.4.0",))
    with Stub(routes) as stub:
        assert fetch(stub) == "sai-cli/v0.4.0"


def test_fetch_past_the_page_limit_is_an_incomplete_lookup():
    """More pages than the limit is an error, never "no release"."""
    routes = paged(*[("sai-rs/v1.0.0",) for _ in range(update.MAX_PAGES + 1)])
    with (
        Stub(routes) as stub,
        pytest.raises(update.UpdateError, match="lookup is incomplete") as caught,
    ):
        fetch(stub)
    assert not isinstance(caught.value, update.NoRelease)
    assert len(stub.requests) == update.MAX_PAGES


@pytest.mark.parametrize(
    "tags",
    [(), ("sai-rs/v1.0.0", "v0.1.0"), ("sai-cli/v0.1.0-rc.1",)],
)
def test_fetch_reports_no_release_without_falling_back(tags):
    with Stub({API_PATH: listing(*tags)}) as stub, pytest.raises(update.NoRelease):
        fetch(stub)
    assert stub.requests == [("GET", API_PATH)]


def test_fetch_skips_releases_flagged_as_prerelease_or_draft():
    for flag in ("prerelease", "draft"):
        routes: Routes = {API_PATH: listing("sai-cli/v0.2.0", **{flag: True})}
        with Stub(routes) as stub, pytest.raises(update.NoRelease):
            fetch(stub)


@pytest.mark.parametrize("status", [403, 429, 500, 503])
@pytest.mark.parametrize(
    "tag",
    [
        "sai-cli/v0.3.1",
        "sai-cli%2Fv0.3.1",
        "sai-rs/v1.0.0",
        "v0.4.0",
        # Tags that end in /releases are releases, not the empty list.
        "docs%2Freleases",
        "docs/releases",
    ],
)
def test_the_web_redirect_never_names_the_latest_release(status, tag):
    """GitHub's repository-wide latest can be an older maintenance release
    or another component, so its tag is not an answer."""
    location = f"https://github.com/{update.REPO}/releases/tag/{tag}"
    routes: Routes = {API_PATH: (status, {}, b"{}"), WEB_PATH: redirect(location)}
    with (
        Stub(routes) as stub,
        pytest.raises(update.UpdateError, match="rate limited or failing") as caught,
    ):
        fetch(stub)
    assert not isinstance(caught.value, update.NoRelease)
    assert stub.requests == [("GET", API_PATH), ("HEAD", WEB_PATH)]


def test_fetch_rejects_malformed_json():
    for body in (b"not json", b'{"tag_name": "sai-cli/v0.2.0"}'):
        with (
            Stub({API_PATH: (200, {}, body)}) as stub,
            pytest.raises(update.UpdateError, match="malformed JSON"),
        ):
            fetch(stub)


def test_a_rate_limited_check_keeps_the_cache_when_a_tag_ends_in_releases():
    update.write_cache(update.Cache("sai-cli/v0.2.0", update.now()))
    location = f"https://github.com/{update.REPO}/releases/tag/docs%2Freleases"
    routes: Routes = {API_PATH: (429, {}, b""), WEB_PATH: redirect(location)}
    with Stub(routes) as stub:
        result = check(stub)
    assert result.exit_code == 1
    assert "rate limited or failing" in result.stderr
    assert update.latest_version() == "0.2.0"


def test_sai_home_is_absolute_even_when_the_variable_is_relative(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SAI_HOME", "relative-home")
    assert update.sai_home() == tmp_path / "relative-home"
    assert update.wheel_cache() == tmp_path / "relative-home" / "cache" / "wheels"


def test_fallback_needs_a_redirect():
    routes: Routes = {API_PATH: (429, {}, b""), WEB_PATH: (200, {}, b"")}
    with (
        Stub(routes) as stub,
        pytest.raises(update.UpdateError, match="without a redirect"),
    ):
        fetch(stub)


def test_fallback_reports_no_release_when_the_page_redirects_to_the_list():
    routes: Routes = {
        API_PATH: (429, {}, b""),
        WEB_PATH: redirect(f"https://github.com/{update.REPO}/releases"),
    }
    with Stub(routes) as stub, pytest.raises(update.NoRelease):
        fetch(stub)


def test_fetch_reports_a_missing_repository_as_an_error():
    with (
        Stub({}) as stub,
        pytest.raises(update.UpdateError, match="HTTP 404") as caught,
    ):
        fetch(stub)
    assert not isinstance(caught.value, update.NoRelease)
    assert stub.requests == [("GET", API_PATH)]


def test_fetch_gives_up_at_the_total_timeout():
    with Stub({API_PATH: listing("sai-cli/v0.2.0")}, delay=1.0) as stub:
        started = time.monotonic()
        with pytest.raises(update.UpdateError, match="timed out"):
            fetch(stub, total_timeout=0.3)
    assert time.monotonic() - started < 0.9


def slow_release() -> tuple[int, dict[str, str], bytes]:
    status, headers, body = listing("sai-cli/v0.2.0")
    return status, headers, body + b" " * 200


@pytest.mark.parametrize("fallback", [False, True])
def test_fetch_keeps_its_deadline_against_a_slow_drip(fallback):
    location = f"/{update.REPO}/releases"
    if fallback:
        routes: Routes = {API_PATH: (429, {}, b""), WEB_PATH: redirect(location)}
        drip_paths = frozenset({WEB_PATH})
    else:
        routes = {API_PATH: slow_release()}
        drip_paths = frozenset({API_PATH})
    with Stub(routes, drip=0.02, drip_paths=drip_paths) as stub:
        started = time.monotonic()
        with pytest.raises(update.UpdateError, match="timed out"):
            fetch(stub, total_timeout=0.3)
        elapsed = time.monotonic() - started
    assert elapsed < 0.6


def test_fetch_reports_an_unreachable_host():
    with Stub({}) as stub:
        url = stub.url
    with pytest.raises(update.UpdateError, match="could not reach GitHub"):
        update.fetch_latest(api_base=url, web_base=url)


# Install kind


def make_prefix(root: Path, direct_url: dict | None, receipt: bool = False) -> Path:
    dist = root / "lib" / "python3.11" / "site-packages" / "sai_cli-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text("Name: sai-cli\nVersion: 0.1.0\n")
    if direct_url is not None:
        (dist / "direct_url.json").write_text(json.dumps(direct_url))
    if receipt:
        (root / "uv-receipt.toml").write_text(
            '[tool]\nrequirements = [{ name = "sai-cli" }]\n'
        )
    return root


GIT = {
    "url": "https://github.com/SaiDotFun/open-sai",
    "vcs_info": {
        "vcs": "git",
        "commit_id": "a" * 40,
        "requested_revision": "sai-cli/v0.1.0",
    },
}


def test_install_kind_uv_tool_from_this_repository(tmp_path):
    prefix = make_prefix(tmp_path / "tools" / "sai-cli", GIT, receipt=True)
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_GIT
    assert (install.commit, install.requested) == ("a" * 40, "sai-cli/v0.1.0")
    assert install.official


DOWNLOADS = "https://github.com/SaiDotFun/open-sai/releases/download"
WHEEL_URL = f"{DOWNLOADS}/sai-cli/v0.1.0/sai_cli-0.1.0-py3-none-any.whl"
WHEEL = {"url": WHEEL_URL, "archive_info": {}}


def test_install_kind_uv_tool_from_a_release_wheel(tmp_path):
    prefix = make_prefix(tmp_path / "tools" / "sai-cli", WHEEL, receipt=True)
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_RELEASE
    assert install.official
    assert install.url == WHEEL_URL
    encoded = {**WHEEL, "url": WHEEL_URL.replace("sai-cli/v", "sai-cli%2Fv")}
    prefix = make_prefix(tmp_path / "encoded", encoded, receipt=True)
    assert update.install_kind(prefix).kind == update.UV_TOOL_RELEASE


def recorded_prefix(root: Path, version: str = "0.1.0", **changes) -> Path:
    """A uv tool install of a local release wheel with its release record."""
    name = update.wheel_name(version)
    direct = {"url": f"file:///tmp/sai-install-x/{name}", "archive_info": {}}
    prefix = make_prefix(root / "tools" / "sai-cli", direct, receipt=True)
    dist = next(prefix.glob("lib/*/site-packages/sai_cli-*.dist-info"))
    (dist / "RECORD").write_text("sai_cli/__init__.py,sha256=abc,0\n")
    record = {
        "schema": 1,
        "tag": update.tag_for(version),
        "sha256": "ab" * 32,
        "installed": update.installed_files_hash(prefix),
        "wheel": None,
        **changes,
    }
    update.release_record_path(prefix).write_text(json.dumps(record))
    return prefix


def test_install_kind_local_release_wheel_needs_its_release_record(tmp_path):
    prefix = recorded_prefix(tmp_path)
    assert update.release_record_path(prefix) == tmp_path / "tools" / (
        ".sai-cli-release.json"
    )
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_RELEASE
    record = update.read_release_record(prefix)
    assert record is not None
    assert (record.tag, record.version) == ("sai-cli/v0.1.0", "0.1.0")

    update.release_record_path(prefix).unlink()
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER


@pytest.mark.parametrize(
    "changes",
    [
        {"tag": "sai-cli/v0.2.0"},
        {"tag": "v0.1.0"},
        {"installed": "cd" * 32},
        {"sha256": "short"},
        {"schema": 2},
        {"wheel": 7},
    ],
)
def test_a_record_that_does_not_describe_the_install_does_not_count(tmp_path, changes):
    prefix = recorded_prefix(tmp_path, **changes)
    assert update.read_release_record(prefix) is None
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER


def test_a_record_does_not_vouch_for_other_files_or_changed_installs(tmp_path):
    """The name and place of a wheel prove nothing; the record must match
    the files that are installed."""
    prefix = recorded_prefix(tmp_path)
    dist = next(prefix.glob("lib/*/site-packages/sai_cli-*.dist-info"))
    direct = json.loads((dist / "direct_url.json").read_text())

    other = {**direct, "url": "file:///tmp/x/evil-1.0.0-py3-none-any.whl"}
    (dist / "direct_url.json").write_text(json.dumps(other))
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER

    # Another wheel with the same name installed over it: RECORD differs.
    (dist / "direct_url.json").write_text(json.dumps(direct))
    assert update.install_kind(prefix).kind == update.UV_TOOL_RELEASE
    (dist / "RECORD").write_text("sai_cli/__init__.py,sha256=other,9\n")
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER


def test_install_kind_release_wheel_follows_the_web_base(tmp_path):
    local = "http://127.0.0.1:9"
    url = WHEEL_URL.replace("https://github.com", local)
    prefix = make_prefix(tmp_path / "local", {**WHEEL, "url": url}, receipt=True)
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER
    assert update.install_kind(prefix, local).kind == update.UV_TOOL_RELEASE


def test_install_kind_uv_tool_from_a_fork_directory_or_index(tmp_path):
    fork = {**GIT, "url": "https://github.com/someone/open-sai.git"}
    local = {"url": "file:///home/u/open-sai", "dir_info": {}}
    wheels = {
        "fork-wheel": WHEEL_URL.replace("SaiDotFun", "someone"),
        "other-host": WHEEL_URL.replace("github.com", "github.com.evil.example"),
        "other-tag": f"{DOWNLOADS}/sai-rs/v0.1.0/sai_cli-0.1.0-py3-none-any.whl",
        "http": WHEEL_URL.replace("https://", "http://"),
        "sdist": WHEEL_URL.replace("-py3-none-any.whl", ".tar.gz"),
    }
    cases = [("fork", fork), ("dir", local), ("index", None)]
    cases += [(name, {**WHEEL, "url": url}) for name, url in wheels.items()]
    for name, direct in cases:
        prefix = make_prefix(tmp_path / name, direct, receipt=True)
        install = update.install_kind(prefix)
        assert install.kind == update.UV_TOOL_OTHER, name
        assert not install.official


def test_install_kind_checkout_uvx_and_other(tmp_path):
    editable = {"url": "file:///src/open-sai", "dir_info": {"editable": True}}
    checkout = make_prefix(tmp_path / "checkout" / ".venv", editable)
    assert update.install_kind(checkout).kind == update.CHECKOUT
    uvx = make_prefix(tmp_path / "cache" / "archive-v0" / "abc123", GIT)
    assert update.install_kind(uvx).kind == update.UVX
    pip = make_prefix(tmp_path / "venv", None)
    assert update.install_kind(pip).kind == update.OTHER


def test_install_kind_strips_credentials_and_survives_corrupt_files(tmp_path):
    secret = {**GIT, "url": "https://user:token@github.com/SaiDotFun/open-sai"}
    prefix = make_prefix(tmp_path / "secret", secret, receipt=True)
    install = update.install_kind(prefix)
    assert install.kind == update.UV_TOOL_GIT
    assert install.url == "https://github.com/SaiDotFun/open-sai"
    assert "token" not in json.dumps(install.to_json())

    broken = make_prefix(tmp_path / "broken", None, receipt=True)
    dist = next(broken.glob("lib/*/site-packages/sai_cli-*.dist-info"))
    (dist / "direct_url.json").write_text("{not json")
    assert update.install_kind(broken).kind == update.UV_TOOL_OTHER


# Cache


def test_installed_version_is_read_from_the_prefix(tmp_path):
    prefix = make_prefix(tmp_path / "tools" / "sai-cli", GIT, receipt=True)
    assert update.installed_version(prefix) == "0.1.0"
    assert update.installed_version(tmp_path / "empty") is None


def test_a_wheel_in_the_cache_is_not_trusted_by_its_path_and_name(tmp_path, sai_home):
    """Only the release record makes a local wheel install a release
    install; where the wheel lies and what it is called prove nothing."""
    cache = update.wheel_cache()
    assert cache == sai_home / "cache" / "wheels"
    kept = {"url": (cache / "sai_cli-0.1.0-py3-none-any.whl").as_uri()}
    prefix = make_prefix(tmp_path / "kept", {**kept, "archive_info": {}}, receipt=True)
    assert update.install_kind(prefix).kind == update.UV_TOOL_OTHER


def test_check_keeps_the_last_update_result(sai_home):
    from sai_cli.core import updater

    updater.write_state({"result": {"status": "failed", "to": "0.2.0"}})
    with Stub({API_PATH: listing("sai-cli/v0.3.0")}) as stub:
        result = check(stub)
    assert result.exit_code == 0, result.output
    state = updater.read_state()
    assert state["latest"] == "sai-cli/v0.3.0"
    assert state["result"] == {"status": "failed", "to": "0.2.0"}


def test_cache_round_trip(sai_home):
    assert update.read_cache() is None
    update.write_cache(update.Cache("sai-cli/v0.2.0", "2026-11-18T00:00:00+00:00"))
    assert update.cache_path() == sai_home / "update.json"
    assert json.loads(update.cache_path().read_text())["schema"] == 1
    assert update.read_cache() == update.Cache(
        "sai-cli/v0.2.0", "2026-11-18T00:00:00+00:00"
    )
    assert update.latest_version() == "0.2.0"


@pytest.mark.parametrize(
    "text",
    [
        "{broken",
        "[]",
        json.dumps({"schema": 2, "latest": "sai-cli/v0.2.0", "checkedAt": "t"}),
        json.dumps({"schema": 1, "latest": "sai-cli/v0.2.0-rc.1", "checkedAt": "t"}),
        json.dumps({"schema": 1, "latest": "v0.2.0", "checkedAt": "t"}),
        json.dumps({"schema": 1, "latest": "sai-cli/v0.2.0"}),
    ],
)
def test_corrupt_cache_counts_as_missing(sai_home, text):
    sai_home.mkdir()
    update.cache_path().write_text(text)
    assert update.read_cache() is None
    assert update.latest_version() is None


# sai update


def invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def check(stub: Stub, *flags: str):
    return invoke(
        *flags, "update", "--check", "--api-base", stub.url, "--web-base", stub.url
    )


def test_update_check_json_reports_versions_and_caches(sai_home):
    tags = ("sai-rs/v100.0.0", "sai-cli/v99.0.0", "sai-cli/v100.0.0-rc.1")
    with Stub({API_PATH: listing(*tags)}) as stub:
        result = check(stub, "--json")
    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["installed"] == package_version()
    assert (report["latest"], report["latestTag"]) == ("99.0.0", "sai-cli/v99.0.0")
    downloads = f"{stub.url}/{update.REPO}/releases/download/sai-cli/v99.0.0"
    assert report["wheelUrl"] == f"{downloads}/sai_cli-99.0.0-py3-none-any.whl"
    assert report["sha256sumsUrl"] == f"{downloads}/SHA256SUMS"
    assert report["updateAvailable"] is True
    assert report["install"]["kind"] in {
        update.CHECKOUT,
        update.UV_TOOL_RELEASE,
        update.UV_TOOL_GIT,
        update.UV_TOOL_OTHER,
        update.UVX,
        update.OTHER,
    }
    assert report["update"]["note"]
    assert update.latest_version() == "99.0.0"


def test_update_check_text_when_current():
    current = update.tag_for(package_version())
    with Stub({API_PATH: listing(current)}) as stub:
        result = check(stub)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith(f"installed  {package_version()} (")
    assert lines[1] == f"latest     {package_version()}"
    assert lines[2] == "sai is up to date."
    assert result.stderr == ""


def test_update_check_with_no_release_is_a_plain_note(sai_home):
    with Stub({API_PATH: listing("sai-rs/v1.0.0")}) as stub:
        text = check(stub)
        data = check(stub, "--json")
    assert text.exit_code == 0, text.output
    assert text.stderr == ""
    lines = text.stdout.splitlines()
    assert lines[0].startswith(f"installed  {package_version()} (")
    assert lines[1:] == [
        "latest     none",
        "No sai-cli release is published yet; nothing to update to.",
    ]
    assert "Error" not in text.output
    assert data.exit_code == 0, data.output
    report = json.loads(data.stdout)
    assert report["latest"] is None
    assert report["latestTag"] is None
    assert report["wheelUrl"] is None
    assert report["updateAvailable"] is False
    assert report["update"] is None
    saved = json.loads(update.cache_path().read_text())
    assert saved["latest"] is None
    assert saved["checkedAt"]


def test_a_withdrawn_release_leaves_the_cache_but_a_failed_check_does_not():
    update.write_cache(update.Cache("sai-cli/v0.2.0", update.now()))
    with Stub({API_PATH: (200, {}, b"garbage")}) as stub:
        assert check(stub).exit_code == 1
    assert update.latest_version() == "0.2.0"
    # The list is now authoritatively empty: the cached version goes too,
    # so mcp_info stops reporting it.
    with Stub({API_PATH: listing()}) as stub:
        assert check(stub).exit_code == 0
    assert update.read_cache() is None
    assert update.latest_version() is None


def test_update_check_failure_exits_1_with_a_json_error_and_no_cache():
    with Stub({API_PATH: (200, {}, b"garbage")}) as stub:
        result = check(stub, "--json")
    assert result.exit_code == 1
    assert result.stderr == ""
    error = json.loads(result.stdout)["error"]
    assert error["code"] == "INTERNAL_ERROR" and error["retryable"] is False
    assert "update check failed" in error["message"]
    assert update.read_cache() is None


LATEST = update.release_for("sai-cli/v0.2.0")


def advice(install: update.Install, capsys) -> tuple[dict, str]:
    report = update_report("0.1.0", LATEST, install)
    print_update_report(report)
    return report["update"], capsys.readouterr().out


@pytest.mark.parametrize("kind", [update.UV_TOOL_RELEASE, update.UV_TOOL_GIT])
def test_an_official_install_is_told_to_run_sai_update(capsys, kind):
    guide, out = advice(update.Install(kind, "/p"), capsys)
    assert guide["command"] is None
    assert f"installed  0.1.0 ({kind})" in out
    assert "Run `sai update` to install it." in out
    assert "install.py" not in out


def test_the_advised_command_names_no_version():
    assert update.INSTALL_COMMAND == (
        "uv run https://raw.githubusercontent.com/SaiDotFun/open-sai/main"
        "/scripts/install.py"
    )


@pytest.mark.parametrize(
    ("install", "command", "text"),
    [
        (update.Install(update.CHECKOUT, "/p"), None, "uv sync"),
        (
            update.Install(update.UVX, "/p"),
            update.INSTALL_COMMAND,
            "Only uv tool installs",
        ),
        (
            update.Install(update.UV_TOOL_OTHER, "/p", "file:///home/u/sai"),
            update.INSTALL_COMMAND,
            "Only uv tool installs",
        ),
        (
            update.Install(update.OTHER, "/p"),
            update.INSTALL_COMMAND,
            "Only uv tool installs",
        ),
    ],
)
def test_other_install_kinds_get_their_own_guidance(capsys, install, command, text):
    guide, out = advice(install, capsys)
    assert guide["command"] == command
    assert text in out
    if command is None:
        assert "install.py" not in out


def test_no_guidance_when_current():
    install = update.Install(update.UV_TOOL_GIT, "/p")
    assert update_report("0.2.0", LATEST, install)["update"] is None
