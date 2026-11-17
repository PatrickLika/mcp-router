"""MCP router: one Claude connector in front of every self-hosted MCP server.

Toward Claude it is a single MCP server with GitHub sign-in. Toward each
server listed in upstreams.yaml it is an MCP client. Every upstream is mounted
under its own prefix (``ha_...``, ``notes_...``), so two servers can never
collide on a tool name.

Configuration is split the same way as the rest of the homelab:

* ``manifests/upstreams.yaml`` -- which servers sit behind the router. Not secret,
  tracked in git, shipped as a ConfigMap.
* environment variables -- the secrets (OAuth app, signing key, upstream
  tokens). Created by hand as a Kubernetes Secret, never committed.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastmcp import FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.server import create_proxy
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.github import GitHubProvider
from starlette.requests import Request
from starlette.responses import PlainTextResponse

log = logging.getLogger("mcp-router")

# Where Claude sends the browser back after sign-in. Anything else trying to
# register as an OAuth client against the router is refused.
DEFAULT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "https://claude.com/api/mcp/auth_callback",
    # Claude Code and Claude Desktop sign in through a local callback.
    "http://localhost:*",
    "http://127.0.0.1:*",
]

INSTRUCTIONS = """\
This is Lika's personal assistant router. Tools are prefixed by the server
they come from (ha_ = Home Assistant, notes_ = the GitHub notes repo, ...).

Guardrails, always:
- Ask before sending email, spending money, or deleting anything.
- Ask before shutting down the gaming PC.
- Email is draft-only unless Lika explicitly says to send it.
"""


@dataclass(frozen=True)
class Upstream:
    name: str
    url: str
    token_env: str | None = None
    enabled: bool = True


def load_upstreams(path: Path) -> list[Upstream]:
    raw = yaml.safe_load(path.read_text()) or {}
    upstreams = [Upstream(**item) for item in raw.get("upstreams", [])]
    names = [u.name for u in upstreams]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate upstream names in {path}: {sorted(dupes)}")
    for u in upstreams:
        if not u.name.isidentifier() or not u.name.islower():
            raise ValueError(f"upstream name {u.name!r} must be a lowercase identifier")
    return upstreams


def build_transport(upstream: Upstream, env: dict[str, str]) -> StreamableHttpTransport | None:
    """Return a transport for the upstream, or None if it cannot be used yet."""
    headers: dict[str, str] = {}
    if upstream.token_env:
        token = env.get(upstream.token_env)
        if not token:
            # Not fatal: the router comes up with the servers it has, so adding
            # a new upstream to the config never takes the others down.
            log.warning("skipping %s: %s is not set", upstream.name, upstream.token_env)
            return None
        headers["Authorization"] = f"Bearer {token}"
    return StreamableHttpTransport(upstream.url, headers=headers)


class AllowlistGitHubProvider(GitHubProvider):
    """GitHub sign-in that only lets named GitHub accounts through.

    Plain GitHub OAuth proves *who* someone is, and any GitHub account can
    complete it. Without this check, anyone who found the URL could sign in
    and turn the lights off.
    """

    def __init__(self, *, allowed_logins: set[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_logins = {login.lower() for login in allowed_logins}

    async def load_access_token(self, token: str) -> AccessToken | None:  # type: ignore[override]
        access = await super().load_access_token(token)
        if access is None:
            return None
        if not is_allowed(access, self._allowed_logins):
            log.warning("refused sign-in from GitHub user %r", (access.claims or {}).get("login"))
            return None
        return access


def is_allowed(access: AccessToken, allowed_logins: set[str]) -> bool:
    login = str((access.claims or {}).get("login") or "").lower()
    return bool(login) and login in allowed_logins


def build_auth(env: dict[str, str]) -> AllowlistGitHubProvider | None:
    mode = env.get("ROUTER_AUTH", "github")
    if mode == "none":
        log.warning("ROUTER_AUTH=none: NO SIGN-IN. Only for local development.")
        return None
    if mode != "github":
        raise ValueError(f"ROUTER_AUTH must be 'github' or 'none', not {mode!r}")

    required = [
        "ROUTER_BASE_URL",
        "GITHUB_CLIENT_ID",
        "GITHUB_CLIENT_SECRET",
        "ROUTER_JWT_SIGNING_KEY",
        "ALLOWED_GITHUB_LOGINS",
    ]
    missing = [k for k in required if not env.get(k)]
    if missing:
        raise ValueError(f"missing required settings: {', '.join(missing)}")

    allowed = {s.strip() for s in env["ALLOWED_GITHUB_LOGINS"].split(",") if s.strip()}
    if not allowed:
        raise ValueError("ALLOWED_GITHUB_LOGINS is empty")

    redirect_uris = env.get("ALLOWED_REDIRECT_URIS")
    return AllowlistGitHubProvider(
        allowed_logins=allowed,
        client_id=env["GITHUB_CLIENT_ID"],
        client_secret=env["GITHUB_CLIENT_SECRET"],
        base_url=env["ROUTER_BASE_URL"],
        # Stable across restarts, so issued tokens and registered clients
        # (Claude) survive a pod restart instead of forcing a re-connect.
        jwt_signing_key=env["ROUTER_JWT_SIGNING_KEY"],
        allowed_client_redirect_uris=(
            [u.strip() for u in redirect_uris.split(",")] if redirect_uris else DEFAULT_REDIRECT_URIS
        ),
        # Only who you are is needed; no access to repos or anything else.
        required_scopes=["read:user"],
    )


def build_router(upstreams: list[Upstream], env: dict[str, str]) -> FastMCP:
    router = FastMCP("lika-assistant", instructions=INSTRUCTIONS, auth=build_auth(env))

    mounted = []
    for upstream in upstreams:
        if not upstream.enabled:
            continue
        transport = build_transport(upstream, env)
        if transport is None:
            continue
        router.mount(create_proxy(transport, name=upstream.name), namespace=upstream.name)
        mounted.append(upstream.name)
    log.info("mounted upstreams: %s", ", ".join(mounted) or "(none)")

    @router.custom_route("/healthz", methods=["GET"])
    async def healthz(_: Request) -> PlainTextResponse:
        # Liveness of the router itself. Deliberately does not call the
        # upstreams: Home Assistant restarting must not restart the router.
        return PlainTextResponse("ok")

    return router


def allowed_hosts(env: dict[str, str]) -> list[str]:
    hosts = ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*"]
    base = env.get("ROUTER_BASE_URL")
    if base:
        hosts.append(urlparse(base).netloc)
    hosts += [h.strip() for h in env.get("EXTRA_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return hosts


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), stream=sys.stdout)
    env = dict(os.environ)
    config = Path(env.get("ROUTER_CONFIG", "/config/upstreams.yaml"))
    router = build_router(load_upstreams(config), env)

    import uvicorn

    app = router.http_app(path="/mcp", allowed_hosts=allowed_hosts(env))
    uvicorn.run(app, host="0.0.0.0", port=int(env.get("PORT", "8000")), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
