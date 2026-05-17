# Development Guide

## Architecture Overview

Press is a distributed system where each node runs a **Node Agent** — a FastAPI-based reverse proxy that serves as the single entry point for all HTTP traffic on that node.

```
Client ──JWT──> Node Agent (port 8080)
                    │
                    ├── validate JWT via JWKS
                    ├── route by longest-prefix match
                    │
                    ├──> Agent A  (UDS: /run/press-node-agent/a.sock)
                    ├──> Agent B  (HTTP: 127.0.0.1:8091)
                    └──> Controlplane  (http://cp:9000)
```

### Key Components

| Component | Purpose |
|-----------|---------|
| **Node Agent** | Reverse proxy, JWT validation, prefix routing, authz caching |
| **Agents** | Business logic services, registered via `.sock` or `.http` files |
| **Controlplane** | Central coordinator, receives forwarded traffic |
| **Authz Server** | External authorization service for resource-level permissions |

### How Agents Are Discovered

Node Agent watches `/run/press-node-agent/` for two file types:

- **`.sock`** — Unix Domain Socket path (production). The socket itself is the registration.
- **`.http`** — Text file containing `host:port` (dev-only). Requires `allow_http_agents: true` in config.

When a file appears, Node Agent calls `GET /_meta/routes` on the agent to learn which URL prefixes it owns. `.sock` always takes precedence over `.http` for the same agent name.

---

## Writing Your Own Agent

### 1. Required Endpoint: `GET /_meta/routes`

Every agent MUST expose this endpoint. It returns the prefixes the agent owns and any public (unauthenticated) paths:

```python
@app.get("/_meta/routes")
async def meta_routes() -> dict:
    return {
        "routes": [
            {
                "prefix": "/myagent",
                "whitelist": [
                    {"path": "/myagent/health"},
                    {"path": "/myagent/public/", "match": "prefix"},
                ],
            },
        ],
    }
```

**Whitelist match modes:**

| Mode | Behavior |
|------|----------|
| `exact` (default) | Only this exact path |
| `exact_or_prefix` | Exact path or anything under it |
| `prefix` | Anything under the path, but NOT the path itself |

### 2. Register with Node Agent

**Production (UDS):**

```python
SOCKET_DIR = Path("/run/press-node-agent")
SOCKET_DIR.mkdir(parents=True, exist_ok=True)
sock = SOCKET_DIR / "myagent.sock"
if sock.exists():
    sock.unlink()
uvicorn.run(app, uds=str(sock))
```

**Development (HTTP):**

```python
SOCKET_DIR.mkdir(parents=True, exist_ok=True)
marker = SOCKET_DIR / "myagent.http"
marker.write_text("127.0.0.1:8091\n")
try:
    uvicorn.run(app, host="127.0.0.1", port=8091)
finally:
    marker.unlink()
```

The agent name is the filename stem (`myagent` from `myagent.sock`).

### 3. Handle Auth Headers

Node Agent stamps every inbound request with auth headers. Your agent MUST check `X-Auth-Source` — if missing, the request bypassed Node Agent and should be denied:

```python
def _check_auth(request: Request) -> tuple[str, str, str, str]:
    src = request.headers.get("x-auth-source")
    if not src:
        raise HTTPException(status_code=401, detail="missing X-Auth-Source")
    sub = request.headers.get("x-auth-sub", "")
    roles = request.headers.get("x-auth-roles", "")
    jti = request.headers.get("x-auth-jti", "")
    return src, sub, roles, jti
```

### 4. Authorization Flow

```
src == "local"          → trusted agent-to-agent call, skip authz
src == "external"       → user request, check roles
  roles has "admin"     → agent-decided shortcut, allow
  roles has "telemetry" → agent-decided shortcut, allow
  roles is "user"       → call Node Agent's admin socket for authz
```

Call the Node Agent admin socket to check permissions:

```python
ADMIN_SOCK = "/run/press-node-agent/node.sock"

async def _ask_node_agent(sub, jti, resource_type, resource_id, action) -> bool:
    transport = httpx.AsyncHTTPTransport(uds=ADMIN_SOCK)
    async with httpx.AsyncClient(transport=transport, base_url="http://node") as c:
        r = await c.post("/_admin/check-permission", json={
            "sub": sub,
            "jti": jti,
            "resource": {"type": resource_type, "id": resource_id},
            "action": action,
        })
    return bool(r.json().get("allowed"))
```

### 5. Path Handling

Node Agent strips the prefix before forwarding. If your agent owns `/myagent` and a request comes for `/myagent/things/1`, your handler sees `/things/1`:

```python
@app.get("/things/{thing_id}")
async def get_thing(thing_id: str, request: Request):
    # path is /things/1, not /myagent/things/1
    ...
```

---

## Security Model

### JWT Authentication

All external traffic to the Node Agent must carry a valid JWT in the `Authorization` header:

```
Authorization: Bearer <jwt>
Authorization: Token <jwt>
```

**Token validation:**

1. Extract the JWT from the `Authorization` header
2. Read the `kid` (key ID) from the JWT header
3. Fetch the corresponding public key from the JWKS endpoint
4. Verify the signature using EdDSA/Ed25519
5. Validate claims: `exp`, `iat`, `sub`, `jti`, `iss`, `aud`

If validation fails, Node Agent returns `401`.

### JWKS (JSON Web Key Set)

Node Agent fetches public keys from the configured JWKS URL:

```json
{
  "jwks": {
    "url": "http://auth-server/.well-known/jwks.json",
    "cache_ttl_seconds": 300
  }
}
```

**Caching behavior:**

- Keys are cached for `cache_ttl_seconds` (default 300s)
- A background task refreshes keys at half the TTL to keep the cache warm
- On signature mismatch, Node Agent invalidates the cache and retries once (handles key rotation)
- Fetch uses serialized locking — concurrent requests don't cause thundering herd
- Readers see either the old or new key set, never a partial state

### Auth Headers Stamped by Node Agent

After JWT validation, Node Agent forwards these headers to agents:

| Header | Value | When |
|--------|-------|------|
| `X-Auth-Source` | `external` | Valid JWT |
| `X-Auth-Source` | `local` | Request from localhost (agent-to-agent) |
| `X-Auth-Sub` | User subject ID | External only |
| `X-Auth-Roles` | Comma-separated roles | External only |
| `X-Auth-Jti` | Token unique ID | External only |

**Security guarantees:**

- These headers are stripped from inbound requests — agents cannot spoof them
- Hop-by-hop headers (`connection`, `transfer-encoding`, etc.) are also stripped
- Whitelisted paths get `X-Auth-Source: external` with empty sub/roles/jti (spec requires the header to be present)

### Authorization (Authz Server)

For resource-level permissions, agents call the Node Agent's admin socket, which proxies to an external authz server:

```
Agent → UDS /_admin/check-permission → Authz Server POST /check
```

**Request format:**

```json
{
  "sub": "user-123",
  "jti": "token-abc",
  "resource": {"type": "Thing", "id": "thing-456"},
  "action": "Read"
}
```

**Response:**

```json
{"allowed": true}
```

**Authz cache:** Node Agent caches authz decisions to reduce load:

- Allowed: cached for 60 seconds
- Denied: cached for 10 seconds
- Max 10,000 entries (LRU eviction)

### Node JWT (Controlplane Authentication)

Traffic forwarded to the controlplane carries a node-level JWT:

```
Authorization: Token <node_jwt>
```

This is configured in `node_jwt` in the config file and is separate from user JWTs.

---

## Running the Development Environment

### 1. Start the Node Agent

```bash
NODE_AGENT_CONFIG=./development/config.json python -m node_agent.main
```

### 2. Start the Dummy Authz Server

```bash
python development/authz_server.py
# Listens on 127.0.0.1:9200, allows everything
```

### 3. Start a Dummy Agent

```bash
# UDS mode (production-like)
python development/dummy_agent.py

# HTTP mode (dev-only, requires allow_http_agents: true in config)
USE_HTTP=1 python development/dummy_agent.py
```

### 4. Test

```bash
# Health check
curl http://127.0.0.1:8080/_meta/health

# Whitelisted path (no JWT required)
curl http://127.0.0.1:8080/dummy/health

# Authenticated request
curl -H "Authorization: Bearer <jwt>" http://127.0.0.1:8080/dummy/things/1
```

### Config Reference

See `development/config.json` for a full example:

| Field | Description |
|-------|-------------|
| `listen.host/port` | Where Node Agent listens for HTTP traffic |
| `jwks.url` | JWKS endpoint for fetching public keys |
| `jwks.cache_ttl_seconds` | How long to cache JWKS keys |
| `jwt.issuer` | Expected `iss` claim in user JWTs |
| `jwt.audience` | Expected `aud` claim in user JWTs |
| `node_jwt` | Token sent to controlplane for node authentication |
| `allow_http_agents` | Allow `.http` file-based agents (dev only) |
| `controlplane.url` | Controlplane base URL |
| `controlplane.prefixes` | URL prefixes routed to controlplane |
| `authz.url` | External authz server URL |
| `socket_dir` | Directory for agent socket files |
| `admin_socket` | UDS path for Node Agent admin socket |
