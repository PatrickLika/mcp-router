"""End-to-end tests: a real upstream MCP server over HTTP, behind the router."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.testclient import TestClient

from router.main import (
    Upstream,
    allowed_hosts,
    build_auth,
    build_router,
    is_allowed,
    load_upstreams,
)

UPSTREAM_TOKEN = "upstream-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def upstream_url():
    """A stand-in for Home Assistant: one tool, behind a bearer token."""
    fake = FastMCP("fake-ha", auth=StaticTokenVerifier({UPSTREAM_TOKEN: {"client_id": "router", "scopes": []}}))

    @fake.tool
    def turn_on(entity_id: str) -> str:
        """Turn an entity on."""
        return f"{entity_id} is on"

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(fake.http_app(path="/api/mcp"), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("fake upstream did not start")
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/api/mcp"
    server.should_exit = True
    thread.join(timeout=5)


DEV_ENV = {"ROUTER_AUTH": "none"}


async def test_tools_are_prefixed_and_calls_reach_the_upstream(upstream_url):
    router = build_router(
        [Upstream(name="ha", url=upstream_url, token_env="HA_TOKEN")],
        {**DEV_ENV, "HA_TOKEN": UPSTREAM_TOKEN},
    )
    async with Client(router) as client:
        names = [t.name for t in await client.list_tools()]
        assert names == ["ha_turn_on"]
        result = await client.call_tool("ha_turn_on", {"entity_id": "light.kitchen"})
        assert result.data == "light.kitchen is on"


async def test_two_upstreams_with_the_same_tool_do_not_collide(upstream_url):
    router = build_router(
        [
            Upstream(name="ha", url=upstream_url, token_env="HA_TOKEN"),
            Upstream(name="other", url=upstream_url, token_env="HA_TOKEN"),
        ],
        {**DEV_ENV, "HA_TOKEN": UPSTREAM_TOKEN},
    )
    async with Client(router) as client:
        assert sorted(t.name for t in await client.list_tools()) == ["ha_turn_on", "other_turn_on"]


async def test_upstream_without_its_token_is_skipped(upstream_url):
    router = build_router([Upstream(name="ha", url=upstream_url, token_env="HA_TOKEN")], DEV_ENV)
    async with Client(router) as client:
        assert await client.list_tools() == []


async def test_disabled_upstream_is_skipped(upstream_url):
    router = build_router(
        [Upstream(name="ha", url=upstream_url, token_env="HA_TOKEN", enabled=False)],
        {**DEV_ENV, "HA_TOKEN": UPSTREAM_TOKEN},
    )
    async with Client(router) as client:
        assert await client.list_tools() == []


def test_healthz_needs_no_sign_in():
    env = {
        "ROUTER_BASE_URL": "https://mcp.example.com",
        "GITHUB_CLIENT_ID": "id",
        "GITHUB_CLIENT_SECRET": "secret",
        "ROUTER_JWT_SIGNING_KEY": "x" * 48,
        "ALLOWED_GITHUB_LOGINS": "PatrickLika",
    }
    app = build_router([], env).http_app(path="/mcp", allowed_hosts=allowed_hosts(env))
    with TestClient(app, base_url="https://mcp.example.com") as http:
        assert http.get("/healthz").text == "ok"
        # And the MCP endpoint itself is closed without a token.
        resp = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Accept": "application/json, text/event-stream"})
        assert resp.status_code == 401
        # Claude discovers sign-in from here.
        assert http.get("/.well-known/oauth-authorization-server").status_code == 200


def test_public_hostname_is_accepted():
    env = {"ROUTER_BASE_URL": "https://mcp.example.com"}
    assert "mcp.example.com" in allowed_hosts(env)


def _token(login: str | None) -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=[], claims={"login": login})


def test_allowlist_matches_case_insensitively():
    allowed = {"patricklika"}
    assert is_allowed(_token("PatrickLika"), allowed)
    assert not is_allowed(_token("someone-else"), allowed)
    assert not is_allowed(_token(None), allowed)
    assert not is_allowed(_token(""), allowed)


def test_github_auth_refuses_to_start_half_configured():
    with pytest.raises(ValueError, match="ALLOWED_GITHUB_LOGINS"):
        build_auth({
            "ROUTER_BASE_URL": "https://mcp.example.com",
            "GITHUB_CLIENT_ID": "id",
            "GITHUB_CLIENT_SECRET": "secret",
            "ROUTER_JWT_SIGNING_KEY": "x" * 48,
        })


def test_auth_defaults_to_github_not_open():
    with pytest.raises(ValueError, match="missing required settings"):
        build_auth({})


def test_unknown_auth_mode_is_rejected():
    with pytest.raises(ValueError):
        build_auth({"ROUTER_AUTH": "off"})


def test_shipped_config_is_valid():
    upstreams = load_upstreams(Path(__file__).parent.parent / "manifests" / "upstreams.yaml")
    assert {u.name for u in upstreams} >= {"ha", "notes"}


def test_duplicate_names_are_rejected(tmp_path):
    cfg = tmp_path / "u.yaml"
    cfg.write_text("upstreams:\n  - {name: ha, url: http://a}\n  - {name: ha, url: http://b}\n")
    with pytest.raises(ValueError, match="duplicate"):
        load_upstreams(cfg)


def test_names_must_be_safe_prefixes(tmp_path):
    cfg = tmp_path / "u.yaml"
    cfg.write_text("upstreams:\n  - {name: Home-Assistant, url: http://a}\n")
    with pytest.raises(ValueError, match="lowercase identifier"):
        load_upstreams(cfg)


async def test_signed_in_stranger_is_refused_at_the_token_layer(monkeypatch):
    from fastmcp.server.auth.providers.github import GitHubProvider

    async def fake_load(self, token):
        return _token({"good": "PatrickLika", "bad": "stranger"}[token])

    monkeypatch.setattr(GitHubProvider, "load_access_token", fake_load)
    auth = build_auth({
        "ROUTER_BASE_URL": "https://mcp.example.com",
        "GITHUB_CLIENT_ID": "id",
        "GITHUB_CLIENT_SECRET": "secret",
        "ROUTER_JWT_SIGNING_KEY": "x" * 48,
        "ALLOWED_GITHUB_LOGINS": "PatrickLika",
    })
    assert (await auth.load_access_token("good")).claims["login"] == "PatrickLika"
    assert await auth.load_access_token("bad") is None
