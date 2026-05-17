# Node Agent

A FastAPI-based reverse proxy that serves as the single entry point on each node. It handles JWT authentication, prefix-based routing to local agents over Unix sockets or HTTP, forwarding to the controlplane, and exposing a local authz check endpoint for agents.

---

## Overview

```
Inbound request
  → Auth middleware        (whitelist check or JWT validation via JWKS)
  → Route dispatcher      (longest-prefix match across unified route table)
      → Agent prefix      → stamp auth headers → proxy to agent Unix socket
      → Controlplane prefix → proxy to controlplane + attach node JWT
      → No match          → 404

Agent-initiated authz check (via admin socket)
  → POST /_admin/check-permission
  → check cache (60s TTL on success)
  → call regional authz server
  → return allow / deny
```

---

## Architecture

### Components

| Component               | Responsibility                                                                           |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| **Config loader**       | Loads `config.json`. Watched via inotify for hot reload.                                 |
| **Agent discovery**     | Watches `/run/press-node-agent/` for agent sockets and loads metadata.                   |
| **Route registry**      | Unified in-memory route table for agents + controlplane. Longest-prefix match.           |
| **Auth middleware**     | Validates inbound JWT via JWKS unless path is whitelisted.                               |
| **JWKS manager**        | Fetches/caches JWKS. Background refresh + transient inline retries.                      |
| **Agent proxy**         | Proxies requests to local Unix sockets or HTTP addresses via httpx. Stamps auth headers. |
| **Controlplane client** | Proxies requests to controlplane with node JWT.                                          |
| **Admin socket server** | Exposes local admin APIs over Unix socket, including authz check endpoint.               |
| **AuthZ client**        | Called by admin socket on agent-initiated permission checks. Caches successes for 60s.   |

---

## Request Flow

```
Request
  │
  ▼
Route lookup (fast path for whitelist check)
  │
  ▼
Whitelisted?
  ├── yes → skip JWT auth
  └── no
        │
        ▼
JWT validation via JWKS
(signature, issuer, audience, expiry)
        │
        ▼
Stamp auth headers
  ├── X-Auth-Source: external
  ├── X-Auth-Sub: <sub>
  ├── X-Auth-Roles: <roles>
  └── X-Auth-Jti: <jti>
        │
        ▼
Proxy to agent
```

---

## Agent Transport

Agents can be reached over two transports.

### Unix socket (default)

```
/run/press-node-agent/node.sock        ← Node Agent admin socket
/run/press-node-agent/compute.sock     ← Compute agent
/run/press-node-agent/volume.sock      ← Volume agent
/run/press-node-agent/network.sock     ← Network agent
```

### HTTP (development only)

For local development, an agent may expose itself over plain HTTP instead of a Unix socket. To register, the agent drops a `.http` file into `/run/press-node-agent/` containing the `address:port` to connect to:

```
/run/press-node-agent/compute.http     ← contains e.g. 127.0.0.1:8081
```

File format — plain text, single line:

```
127.0.0.1:8081
```

Node Agent reads the address from the file and connects over HTTP. Everything else — `/_meta/routes` discovery, route validation, auth header stamping — works identically to Unix socket agents.

HTTP agents are **disabled by default**. They must be explicitly enabled via config:

```json
{
  "allow_http_agents": true
}
```

If `allow_http_agents` is `false` (the default), any `.http` file found in the directory is rejected and logged as an error. No routes are registered for that agent.

If both `compute.sock` and `compute.http` exist for the same agent, the Unix socket takes precedence and the `.http` file is ignored.

inotify watches `.http` files the same way as `.sock` files — no changes to the discovery mechanism.

---

Socket filename (without extension) defines the canonical agent identity in both cases.

Examples:

* `compute.sock` or `compute.http` → agent identity `compute`
* `volume.sock` or `volume.http` → agent identity `volume`

This agent identity is used for:

* admin refresh operations

Agent identity has no bearing on what prefixes an agent may declare. Agents freely declare their own prefixes via `/_meta/routes`. A `volume.sock` agent may own `/snapshots`, `/backups`, or any other prefix.

---

## Agent Discovery

Node Agent discovers agents through:

* startup scan
* inotify monitoring
* periodic reconciliation

Both `*.sock` and `*.http` files are discovered. inotify watches the entire `/run/press-node-agent/` directory — no separate mechanism for HTTP agents.

inotify is for responsiveness. Reconciliation is authoritative. Missed events, startup races, or kernel queue overflow are corrected during reconciliation.

---

### Startup Scan

On startup:

1. Scan `/run/press-node-agent/`
2. Find all `*.sock` and `*.http` files except `node.sock`
3. For each agent: if both `.sock` and `.http` exist, use `.sock` and ignore `.http`
4. If `.http` found and `allow_http_agents: false`, reject and log error
5. Query each reachable agent for metadata
6. Validate metadata
7. Register routes

---

### inotify

On filesystem events:

| Event                      | Action                                                             |
| -------------------------- | ------------------------------------------------------------------ |
| New `.sock` or `.http`     | Query metadata, register routes (subject to transport rules above) |
| `.sock` or `.http` removed | Deregister owned routes                                            |

---

### Reconciliation

Every 5 minutes:

* discover newly added sockets
* remove stale sockets
* correct missed inotify events

Known agents are not refreshed automatically. Route refresh happens via admin API or config reload.

---

## Agent Metadata API

Each agent must expose:

```
GET /_meta/routes
```

over its Unix socket.

Example:

```json
{
  "routes": [
    {
      "prefix": "/compute",
      "whitelist": [
        { "path": "/compute/health" },
        { "path": "/compute/metrics", "match": "exact_or_prefix" },
        { "path": "/compute/stream/", "match": "prefix" }
      ]
    },
    {
      "prefix": "/compute/v2",
      "whitelist": [
        { "path": "/compute/v2/health" }
      ]
    }
  ]
}
```

A single agent can declare multiple prefixes. There is no constraint on which prefixes an agent may claim — a `volume.sock` agent may legitimately declare `/snapshots`, `/backups`, etc.

---

## Route Validation Rules

Node Agent enforces the following on every `/_meta/routes` response:

* prefix must not be `/`
* prefix must not be `/*`
* prefix must not overlap with any controlplane prefix defined in `config.json`

Invalid entries are rejected and logged. Valid entries from the same response continue to register.

---

## Conflict Handling

If two agents declare the same prefix, the first registered wins. The conflicting prefix from the second agent is rejected and logged loudly. Non-conflicting prefixes from the second agent are registered normally.

This is treated as deployment misconfiguration.

---

## Unified Routing Model

All routes are merged into one route table. Each entry contains:

* prefix
* destination type
* destination target
* owning agent (if agent route)
* whitelist rules

Example:

```
/compute        → compute.sock
/snapshots      → volume.sock
/api/v1/events  → controlplane
```

Routing uses **longest-prefix match**.

Example: `/compute/v2/jobs` matches `/compute/v2` before `/compute`.

---

## Prefix Stripping

Agent routes are forwarded after prefix stripping.

Example:

Inbound: `/compute/api/list`

Forwarded to compute agent as: `/api/list`

---

## Controlplane Routing

Controlplane prefixes are defined in config:

```json
{
  "controlplane": {
    "url": "https://controlplane.example.com",
    "prefixes": [
      "/api/v1/cluster",
      "/api/v1/events"
    ]
  }
}
```

Matching requests are forwarded to the controlplane with the node JWT attached:

```
Authorization: Token <node-jwt>
```

---

## Authentication

All non-whitelisted requests require:

```
Authorization: Token <jwt>
```

JWT validation covers:

* EdDSA (Ed25519) signature validation via JWKS
* issuer validation
* audience validation
* expiry validation

`kid` in the JWT header is used to select the matching public key from JWKS, enabling key rotation.

JWKS fetched from `jwks.url`.

---

## JWT Structure

Node Agent uses Ed25519 (`alg = "EdDSA"`) signed JWTs.

### Header

```json
{
  "alg": "EdDSA",
  "kid": "auth-key-v1",
  "typ": "JWT"
}
```

`kid` identifies the signing key and enables key rotation via JWKS.

### Payload

```json
{
  "sub": "user_123",
  "roles": ["user"],
  "jti": "uuid-v4",
  "iat": 1747350000,
  "exp": 1747353600
}
```

| Claim   | Purpose                                            |
| ------- | -------------------------------------------------- |
| `sub`   | identity                                           |
| `roles` | coarse-grained role (`admin`, `telemetry`, `user`) |
| `jti`   | token identifier                                   |
| `iat`   | issued-at                                          |
| `exp`   | expiration                                         |

### Roles

| Role        | Meaning                                                                     |
| ----------- | --------------------------------------------------------------------------- |
| `admin`     | Full access. No authz server call needed.                                   |
| `telemetry` | Restricted to telemetry resources and actions. No authz server call needed. |
| `user`      | Normal users. Authz delegated to the regional authz server by the agent.    |

Node Agent does **not** call the authz server itself. It validates the JWT and passes identity downstream. Role-based fast-paths (`admin`, `telemetry`) and authz server calls are the agent's responsibility, initiated via the admin socket.

---

## Auth Headers

On every proxied request to an agent, Node Agent stamps the following headers.

### External requests (JWT-authenticated)

```
X-Auth-Source: external
X-Auth-Sub: user_123
X-Auth-Roles: user
X-Auth-Jti: uuid-v4
```

### Agent-to-agent requests (local traffic)

```
X-Auth-Source: local
```

No `sub`, `roles`, or `jti` are stamped on local traffic. The receiving agent sees `X-Auth-Source: local` and skips authz entirely — agent-to-agent calls are trusted within the node.

---

## Agent Authorization

Node Agent validates JWT integrity only. It stamps auth headers and proxies the request. Agents are responsible for authorizing the request against their own resources.

The `roles` claim is passed to agents via `X-Auth-Roles`. Agents decide how to handle each role — including whether to call `/_admin/check-permission` or handle it locally. Node Agent imposes no fast-path logic based on role.

### User role — authz server check

When an agent needs to authorize a resource action, it calls Node Agent's admin socket:

```
POST /_admin/check-permission
```

Request body:

```json
{
  "sub": "user_123",
  "jti": "uuid-v4",
  "resource": {
    "type": "Container",
    "id": "container-456"
  },
  "action": "Create"
}
```

Node Agent checks its local cache first, then forwards to the regional authz server if not cached.

Response:

```json
{ "allowed": true }
```

or

```json
{ "allowed": false }
```

The agent acts on the response — returning `403 Forbidden` to the caller if denied.

### Cache behavior

| Result                   | TTL                     |
| ------------------------ | ----------------------- |
| `allowed: true`          | 60s                     |
| `allowed: false`         | 10s                     |
| Authz server unreachable | not cached — return 503 |

Cache key: `(jti, sub, resource_type, resource_id, action)`

Including `jti` ensures a fresh token always gets a fresh check — results from a previous token are never reused.

Cache lives in Node Agent only. Agents do not cache authz results across requests.

### Agent-to-agent calls

When `X-Auth-Source: local` is present, the receiving agent skips authz entirely. No admin socket call is made.

### Missing X-Auth-Source

Agents must deny any request where `X-Auth-Source` is absent. This header is always stamped by Node Agent — its absence indicates the request bypassed Node Agent entirely, which should not be possible given socket permissions but must be handled defensively.

---

## Whitelist Semantics

Whitelist entries support 3 match modes.

---

### exact (default)

Matches only the exact path.

```json
{ "path": "/compute/health", "match": "exact" }
```

Matches: `/compute/health`

Does NOT match: `/compute/health/live`

---

### exact_or_prefix

Matches exact path or any sub-path.

```json
{ "path": "/compute/metrics", "match": "exact_or_prefix" }
```

Matches: `/compute/metrics`, `/compute/metrics/live`

Does NOT match: `/compute/metricsz`

---

### prefix

Matches sub-paths only. The entry itself is not matched.

```json
{ "path": "/compute/stream/", "match": "prefix" }
```

Matches: `/compute/stream/live`

Does NOT match: `/compute/stream`

---

The `/` separator is load-bearing in all prefix checks. `/compute/health` with `exact_or_prefix` matches `/compute/health` and `/compute/health/live` but not `/compute/healthz`.

---

## Startup Behavior

Node Agent starts immediately. It does not wait for all agents.

If a request reaches a known agent whose metadata is not yet loaded:

* inline metadata fetch is attempted
* failure → require auth (fail safe, not fail open)
* unreachable agent → `503 Service Unavailable`

Unknown routes return `404 Not Found` until the reconciliation loop discovers them.

---

## Configuration

Config path read from `NODE_AGENT_CONFIG` env var. Default: `./config.json`.

```json
{
  "listen": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "jwks": {
    "url": "https://controlplane.example.com/.well-known/jwks.json",
    "cache_ttl_seconds": 300
  },
  "node_jwt": "<node-jwt>",
  "allow_http_agents": false,
  "controlplane": {
    "url": "https://controlplane.example.com",
    "prefixes": [
      "/api/v1/cluster",
      "/api/v1/events"
    ]
  }
}
```

Agent registration is fully driven by socket discovery — there is no `agents` block in config.

---

## Config Reload

Hot reload supported via inotify.

Reloadable:

* JWKS config
* controlplane config
* node JWT
* `allow_http_agents` — toggling this off will deregister all currently active HTTP agents

Non-reloadable (require restart):

* listen host
* listen port

Reload behavior:

1. Parse and validate new config
2. Atomically swap route table
3. Allow in-flight requests to complete against old table
4. Refresh all agent metadata caches

---

## JWKS Management

JWKS is cached in memory with a configurable TTL. A background worker keeps the cache warm.

Cache is also invalidated on signature verification failure to handle key rotation.

On transient fetch failure:

* retry inline 3–4 times
* if all retries fail → `503 Service Unavailable`

---

## Admin Socket

Local admin API exposed at:

```
/run/press-node-agent/node.sock
```

No network exposure. Trust boundary is the local root environment — no additional auth applied.

---

### Refresh Agent Metadata

```
POST /_admin/refresh?agent=<socket-filename>
```

Example:

```bash
curl --unix-socket /run/press-node-agent/node.sock \
  -X POST "http://localhost/_admin/refresh?agent=compute.sock"
```

Behavior:

* invalidate cached metadata for that agent
* re-fetch `/_meta/routes`
* rebuild route entries

Intended to be called from deployment tooling after an agent update:

```bash
systemctl restart compute-agent
curl --unix-socket /run/press-node-agent/node.sock \
  -X POST "http://localhost/_admin/refresh?agent=compute.sock"
```

---

### Check Permission

```
POST /_admin/check-permission
```

Called by agents to authorize a user action against a resource. Node Agent checks its local cache first, then forwards to the regional authz server if not cached.

Request body:

```json
{
  "sub": "user_123",
  "jti": "uuid-v4",
  "resource": {
    "type": "Container",
    "id": "container-456"
  },
  "action": "Create"
}
```

Response:

```json
{ "allowed": true }
```

Cache behavior:

* `allowed: true` — cached for 60s, keyed on `(jti, sub, resource_type, resource_id, action)`
* `allowed: false` — cached for 10s, same key
* authz server unreachable — not cached, return 503

This endpoint is only called when agents need to authorize a resource action. Agents decide independently whether to call it based on the role in `X-Auth-Roles`.

---

## Agent Deployment

Agents can run as systemd services or containers (Docker/Podman). Node Agent does not care which — it discovers agents purely through the socket directory.

---

### systemd

Socket activation is supported for systemd-managed agents.

Example unit snippet:

```ini
[Socket]
ListenStream=/run/press-node-agent/compute.sock
SocketMode=0600

[Install]
WantedBy=sockets.target
```

Benefits:

* lazy startup
* crash recovery via systemd reactivation on next connection
* no explicit liveness management in Node Agent — connection errors return `502 Bad Gateway`

Socket activation does not apply to containerized agents.

---

### Containers (Docker / Podman)

Containerized agents bind-mount `/run/press-node-agent/` from the host into the container at the same path. The agent drops its `.sock` (or `.http` in dev) file into that directory on startup, and Node Agent discovers it via inotify as normal.

Example (Docker):

```bash
docker run \
  -v /run/press-node-agent:/run/press-node-agent \
  compute-agent
```

Container restart policies handle crash recovery — Node Agent treats connection errors as `502 Bad Gateway` and makes no assumptions about agent liveness.

Node Agent itself must also have `/run/press-node-agent/` mounted if running as a container.

---

## Error Handling

| Condition                  | Response                                               |
| -------------------------- | ------------------------------------------------------ |
| Invalid / missing JWT      | 401                                                    |
| AuthZ server denied        | 403 (returned by agent, not Node Agent)                |
| No route match             | 404                                                    |
| Backend connection failure | 502                                                    |
| Agent unavailable          | 503                                                    |
| JWKS unavailable           | 503                                                    |
| AuthZ server unavailable   | 503 (returned by agent after failed admin socket call) |
| Backend timeout            | 504                                                    |

Backend error response bodies are passed through to the caller unchanged.

---

## Security Model

Enforced protections:

* controlplane prefix protection (agents cannot overlap)
* JWT signature, issuer, audience, and expiry validation (Ed25519 / EdDSA)
* whitelist validation at fetch time (agents cannot declare root paths or controlplane-owned paths as public)
* slash-boundary enforcement in all prefix/whitelist checks
* auth headers stripped and re-stamped by Node Agent on every proxied request — agents cannot spoof `X-Auth-Source`, `X-Auth-Sub`, `X-Auth-Roles`, or `X-Auth-Jti`

---

## Trust Model

Deployment assumptions:

* dedicated trusted node
* root-controlled environment
* non-root processes cannot access sockets under `/run/press-node-agent/`
* agents are first-party reviewed code
* local socket boundary is the trust boundary — no additional in-process auth

This is not a hostile multi-tenant zero-trust design.

---

## Dependencies

| Package         | Purpose                                          |
| --------------- | ------------------------------------------------ |
| `fastapi`       | Web framework                                    |
| `uvicorn`       | ASGI server                                      |
| `httpx`         | Async HTTP client with Unix socket (UDS) support |
| `pyjwt[crypto]` | JWT decode and validation                        |
| `cryptography`  | RSA/EC key handling for JWKS                     |
| `pydantic`      | Config validation                                |

---

## Environment Variables

| Variable            | Default         | Description         |
| ------------------- | --------------- | ------------------- |
| `NODE_AGENT_CONFIG` | `./config.json` | Path to config file |

---