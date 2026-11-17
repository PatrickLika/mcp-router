# mcp-router

One Claude connector in front of every self-hosted MCP server on the Pi.

```
 Claude (phone, web, desktop)
        │  HTTPS, GitHub sign-in
        ▼
 tunnel ─▶ mcp-router (namespace mcp) ──▶ Home Assistant   /api/mcp      tools: ha_*
                                     └──▶ GitHub notes repo (hosted)     tools: notes_*
                                     └──▶ … next servers from the roadmap
```

- **One sign-in, in one place.** Claude signs in with GitHub. Only the accounts
  in `ALLOWED_GITHUB_LOGINS` get through. Anyone else can finish GitHub's
  login page and is then refused by the router.
- **No tool name collisions.** Every server's tools carry its prefix
  (`ha_HassTurnOn`, `notes_get_file_contents`).
- **Adding a server is a config change.** Add an entry to
  [`manifests/upstreams.yaml`](manifests/upstreams.yaml), open a PR, merge.
  An entry whose token does not exist yet is skipped, not fatal.

Written in Python on [FastMCP](https://gofastmcp.com) (roadmap decisions from
Nov 17: Python for custom servers, OAuth on the router, everything behind it).

---

## Setup (once)

### 1. GitHub OAuth app (the sign-in)

GitHub → Settings → Developer settings → OAuth Apps → **New OAuth App**

| Field | Value |
|---|---|
| Homepage URL | `https://mcp.likasolutions.com` |
| Authorization callback URL | `https://mcp.likasolutions.com/auth/callback` |

Generate a client secret. You need the client ID and secret in step 4.

### 2. Home Assistant token (roadmap step 2)

In Home Assistant:

1. **Settings → Devices & services → Add integration → Model Context Protocol
   Server.** Keep the default "Assist" API.
2. **Settings → Voice assistants → Expose** — only these entities are visible
   to Claude. Expose the lights, heating, plugs and the gaming PC switches.
3. Your user profile → **Security → Long-lived access tokens → Create**.
   Name it `mcp-router`.

Check the token works from the cluster before going further:

```bash
kubectl run -it --rm hacheck --image=curlimages/curl --restart=Never -- \
  curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer <token>" -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"check","version":"0"}}}' \
  http://home-assistant.default.svc.cluster.local:8123/api/mcp
```

`200` means good. `401` is the token. `404` means the integration is not
enabled. If Home Assistant no longer runs in the cluster (e.g. it moved to
Home Assistant OS on the Pi 5), change the `ha` URL in
[`manifests/upstreams.yaml`](manifests/upstreams.yaml) to its LAN address.

### 3. Notes repo token (roadmap step 1)

GitHub → Settings → Developer settings → **Fine-grained tokens → Generate**

- Repository access: **Only select repositories** → the notes repo, nothing else
- Permissions: **Contents: Read and write**, Metadata: Read (automatic)
- Expiration: a year, with a calendar reminder to renew it

This token can touch the notes repo and nothing else, even if it leaked.

### 4. The Secret

```bash
kubectl create namespace mcp
```

```bash
kubectl -n mcp create secret generic mcp-router-secrets \
  --from-literal=GITHUB_CLIENT_ID=<from step 1> \
  --from-literal=GITHUB_CLIENT_SECRET=<from step 1> \
  --from-literal=ROUTER_JWT_SIGNING_KEY="$(openssl rand -hex 32)" \
  --from-literal=HA_TOKEN=<from step 2> \
  --from-literal=GITHUB_NOTES_TOKEN=<from step 3>
```

To change one value later, delete and recreate the Secret, then
`kubectl -n mcp rollout restart deploy/mcp-router`. Changing
`ROUTER_JWT_SIGNING_KEY` signs everyone out, which is the way to revoke access.

### 5. First image

Before the first merge, turn on **Settings → Actions → General → Allow GitHub
Actions to create and approve pull requests** for this repo.

Merging to `main` runs [`build.yml`](.github/workflows/build.yml): tests, an
arm64 image at `ghcr.io/patricklika/mcp-router:sha-…`, and a PR pinning that
tag. Merge the PR.

The package is private on first push. Either make it public (GitHub → your
profile → Packages → mcp-router → Package settings → Change visibility; the
image holds no secrets), or add an `imagePullSecret` for GHCR.

### 6. Argo CD

```bash
kubectl apply -f argocd/application.yaml
```

```bash
kubectl -n mcp get pods,svc,pvc -w
```

```bash
kubectl -n mcp logs deploy/mcp-router
```

The log line `mounted upstreams: ha, notes` is the one you want. A
`skipping …: … is not set` line names a missing Secret key.

### 7. Tunnel

Add a public hostname to the existing tunnel:

| Hostname | Service |
|---|---|
| `mcp.likasolutions.com` | `http://mcp-router.mcp.svc.cluster.local:8000` |

If the tunnel connector does not run inside the cluster, it cannot resolve
that name. Point it at Traefik instead and add an Ingress for the host, as
`deployments/homeassistant/ingress.yaml` in HomeLab-Config does.

Check from anywhere:

```bash
curl -s https://mcp.likasolutions.com/healthz
```

```bash
curl -s https://mcp.likasolutions.com/.well-known/oauth-protected-resource/mcp
```

The second must show `https://mcp.likasolutions.com/mcp` as `resource`. If it
shows `http://` or another host, `ROUTER_BASE_URL` in
[`manifests/router.env`](manifests/router.env) is wrong.

### 8. Connect Claude

claude.ai → Settings → Connectors → **Add custom connector**

- Name: `Assistant`
- URL: `https://mcp.likasolutions.com/mcp`

Connect, sign in with GitHub, approve. The connector then works on the phone
app too. Try: *"which lights are on?"* and *"read the summary note"*.

---

## Day to day

| I want to… | Do this |
|---|---|
| Add an MCP server | Add it to [`manifests/upstreams.yaml`](manifests/upstreams.yaml), add its token to the Secret, PR, merge |
| Turn one off for a while | `enabled: false` on its entry |
| Let someone else in | Add their GitHub login to `ALLOWED_GITHUB_LOGINS` in [`manifests/router.env`](manifests/router.env) |
| Sign everyone out | New `ROUTER_JWT_SIGNING_KEY`, restart |
| Read logs | `kubectl -n mcp logs -f deploy/mcp-router` |
| Run it on a laptop | See below |

### Local run, no sign-in

```bash
pip install -r requirements-dev.txt
```

```bash
ROUTER_AUTH=none ROUTER_CONFIG=manifests/upstreams.yaml HA_TOKEN=<token> python -m router.main
```

`ROUTER_AUTH=none` exists for this only; CI fails any PR that puts it in the
manifests.

```bash
pytest -q
```

The tests run a real MCP server over HTTP behind the router: prefixing,
collisions, skipped upstreams, the 401 without a token, and the allowlist.

---

## Security notes

- The Service is ClusterIP and CI keeps it that way. The tunnel is the only way in.
- GitHub sign-in asks for `read:user` only, which is enough to learn who you are and nothing more.
- Only Claude's own callback URLs (and localhost, for Claude Code and Desktop)
  can register as OAuth clients.
- Each upstream token is as narrow as that server allows: an HA user token,
  and a fine-grained PAT for the notes repo only.
- The guardrails from the roadmap (ask before sending email, spending money,
  deleting, or shutting down the gaming PC) are sent to Claude as server
  instructions. They are instructions, not enforcement. Enforcement for the PC
  belongs in Home Assistant: leave the shutdown script unexposed until a
  confirm step exists.
