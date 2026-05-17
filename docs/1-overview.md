# Overview

Node Agent is a reverse proxy and service discovery layer that routes incoming HTTP requests to registered agents, controlplanes, or an authorization service. It sits at the edge of a node and manages the trust boundary between external clients and internal services.

## Architecture

<img src="assets/overall-design.png" alt="Overall Design" width="700">

## Two Servers

Node Agent runs two independent FastAPI applications:

| Server | Transport | Purpose |
|---|---|---|
| **Proxy App** | TCP (`host:port`) | Receives external HTTP traffic, validates JWTs, routes to agents/controlplanes |
| **Admin + Local Proxy** | Unix Domain Socket (`node.sock`) | Internal-only: admin endpoints (`/_admin/*`) **and** full proxy routing for agents. Agents use this to reach controlplanes, authz, or other agents without managing their own credentials- Node Agent attaches the correct JWT downstream. |

```mermaid
graph TB
    subgraph "node.sock"
        Admin["Admin endpoints"]
        LocalProxy["Local proxy"]
    end

    Agent["Agent"] -->|POST| Admin
    Agent -->|GET| LocalProxy

    Admin --> Authz["authz service"]
    LocalProxy -->|attaches node JWT| CP["Controlplane"]
    LocalProxy -->|stamps X-Auth-Source: local| Other["Other Agent"]
```

## Request Flow

```mermaid
sequenceDiagram
    participant C as External Client
    participant P as Proxy App (TCP)
    participant RR as Route Registry
    participant Auth as JWT Validator
    participant PC as Proxy Client
    participant A as Agent

    C->>P: GET /agent-prefix/api/resource
    P->>RR: lookup("/agent-prefix/api/resource")
    RR-->>P: RouteEntry{prefix, dest_type, target}

    alt dest_type == "agent"
        alt path is whitelisted
            Auth-->>P: no auth required
        else
            P->>Auth: validate JWT
            Auth-->>P: AuthResult{sub, roles, jti}
        end
        P->>PC: proxy_to_agent + auth headers
        PC->>A: forward request
        A-->>PC: response
        PC-->>P: response
        P-->>C: response
    end
```

## Route Types

Every request is matched against the **Route Registry** which contains three destination types:

| `dest_type` | Description | Who can access |
|---|---|---|
| **agent** | Routes registered by agents via socket discovery | External (with JWT) or local (no auth) |
| **controlplane** | Routes configured in `config.json` under `controlplanes` | External (with JWT, forwarded) or local (node credentials) |
| **authz** | Routes configured in `config.json` under `authz` | **Local only**- returns 404 to external callers |

Routes are matched using **longest-prefix matching**. When an agent registers a prefix like `/my-agent`, a request to `/my-agent/things/123` is forwarded to that agent with the path stripped to `/things/123`.

## Authentication Scenarios

### Scenario 1: External Client -> Agent (non-whitelisted route)

```mermaid
sequenceDiagram
    participant C as External Client
    participant P as Proxy App
    participant Auth as JWT/JWKS
    participant A as Agent

    C->>P: GET /my-agent/things/1<br/>Authorization: Bearer <jwt>
    P->>Auth: validate JWT (issuer, audience, signature)
    Auth-->>P: AuthResult{source:"external", sub:"alice", roles:"user", jti:"..."}
    P->>A: GET /things/1<br/>X-Auth-Source: external<br/>X-Auth-Sub: alice<br/>X-Auth-Roles: user<br/>X-Auth-Jti: ...
    A-->>P: 200 OK
    P-->>C: 200 OK
```

The agent receives `X-Auth-*` headers and must perform its own authorization check (see [Writing an Agent](4-write-agent.md)).

### Scenario 2: External Client -> Agent (whitelisted route)

```mermaid
sequenceDiagram
    participant C as External Client
    participant P as Proxy App
    participant A as Agent

    C->>P: GET /my-agent/health
    P->>P: path matches whitelist entry
    P->>A: GET /health (no X-Auth-* headers)
    A-->>P: 200 OK
    P-->>C: 200 OK
```

Whitelisted paths bypass JWT validation entirely. No auth headers are forwarded. Use this for health checks and public endpoints.

### Scenario 3: External Client -> Controlplane

```mermaid
sequenceDiagram
    participant C as External Client
    participant P as Proxy App
    participant Auth as JWT/JWKS
    participant CP as Controlplane

    C->>P: GET /api/v1/cluster/nodes<br/>Authorization: Bearer <user-jwt>
    P->>Auth: validate JWT
    Auth-->>P: valid
    P->>CP: GET /api/v1/cluster/nodes<br/>Authorization: Bearer <user-jwt> (forwarded as-is)
    CP-->>P: 200 OK
    P-->>C: 200 OK
```

The user's original JWT is forwarded to the controlplane so it can identify the caller.

### Scenario 4: Local Agent -> Controlplane (via node.sock)

```mermaid
sequenceDiagram
    participant A as Agent
    participant N as Local Proxy (node.sock)
    participant CP as Controlplane

    A->>N: GET /api/v1/cluster/nodes (over UDS, no auth needed)
    N->>N: route lookup -> controlplane "primary"
    N->>CP: GET /api/v1/cluster/nodes<br/>Authorization: Token <node-jwt> (node attaches its own creds)
    CP-->>N: 200 OK
    N-->>A: 200 OK
```

Agents route controlplane requests through `node.sock` without any credentials. The local proxy on `node.sock` looks up the route, and Node Agent attaches its own JWT (`controlplanes[].jwt_token`) downstream. The agent never needs to store or manage controlplane credentials.

### Scenario 5: Local Agent -> Authz (via node.sock)

```mermaid
sequenceDiagram
    participant A as Agent
    participant N as Admin App (node.sock)
    participant AZ as Authz Service

    A->>N: POST /_admin/has-permission<br/>{sub, jti, resource, action}
    N->>N: check local cache
    N->>AZ: POST /check (with node jwt)
    AZ-->>N: {allowed: true}
    N-->>A: {allowed: true}
```

Authz is **only** accessible via the local admin socket. External requests to authz prefixes receive a 404.

### Scenario 6: External Client -> Authz

```mermaid
sequenceDiagram
    participant C as External Client
    participant P as Proxy App

    C->>P: GET /authz/check
    P->>P: route.dest_type == "authz" && !is_local
    P-->>C: 404 Not Found
```

Authz routes are hidden from external callers entirely.

### Scenario 7: Agent-to-Agent (local UDS call)

```mermaid
sequenceDiagram
    participant A1 as Agent A
    participant N as Local Proxy (node.sock)
    participant A2 as Agent B

    A1->>N: GET /agent-b/some/path (over UDS, no auth needed)
    N->>N: route lookup -> agent "agent-b"
    N->>A2: GET /some/path<br/>X-Auth-Source: local
    A2-->>N: 200 OK
    N-->>A1: 200 OK
```

When one agent calls another through `node.sock`, the local proxy routes the request and adds `X-Auth-Source: local`. The target agent should trust this and skip authorization.

## Auth Header Reference

When the proxy forwards to an agent, it adds these headers:

| Header | Value | When |
|---|---|---|
| `X-Auth-Source` | `external` | External client with valid JWT |
| `X-Auth-Source` | `local` | Call via `node.sock` (local proxy adds this for agent-to-agent, agent-to-controlplane, or admin calls) |
| `X-Auth-Sub` | JWT `sub` claim | External only |
| `X-Auth-Roles` | Comma-separated `roles` | External only |
| `X-Auth-Jti` | JWT `jti` claim (token ID) | External only |

**If `X-Auth-Source` is missing, the request bypassed Node Agent and should be denied.**

## Agent Discovery

Agents are discovered automatically from the socket directory:

- **`.sock` files**- Unix domain socket path (production). Agent name = filename stem.
- **`.http` files**- Contains `host:port` on first line (dev-only). Requires `allow_http_agents: true` in config.
- If both `.sock` and `.http` exist for the same agent, `.sock` wins.

On discovery, Node Agent calls `GET /_meta/routes` on the agent to learn its prefixes and whitelisted paths.

## Configuration Hot Reload

Node Agent watches `config.json` for changes. On modification:
- JWKS URL/TTL updates with key cache invalidation
- Controlplane and authz routes are rebuilt
- Agent routes are preserved
- Listen host/port changes are **ignored** (require restart)
