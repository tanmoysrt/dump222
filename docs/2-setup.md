# Setup

Node Agent requires Python 3.14+. It can be run directly on the host (pythonic) or inside a container (Docker).


## Prerequisites

- Python 3.14+
- A JWKS endpoint serving Ed25519 public keys
- A controlplane service (optional)
- An authorization service (optional)

## Configuration File

Node Agent is configured via a JSON file. The default path is `./config.json`, overridden by the `NODE_AGENT_CONFIG` environment variable.

### Full Configuration Reference

```json
{
  "listen": {
    "host": "0.0.0.0",
    "port": 8080
  },
  "jwks": {
    "url": "http://127.0.0.1:9100/.well-known/jwks.json",
    "cache_ttl_seconds": 300
  },
  "jwt": {
    "issuer": "press-controlplane",
    "audience": "press-node"
  },
  "allow_http_agents": false,
  "controlplanes": [
    {
      "name": "primary",
      "url": "http://127.0.0.1:9000",
      "prefixes": ["/api/v1/cluster", "/api/v1/events"],
      "jwt_token": "replace-with-node-jwt"
    }
  ],
  "authz": {
    "url": "http://127.0.0.1:9200/check",
    "jwt_token": ""
  },
  "socket_dir": "/run/press-node-agent",
  "admin_socket": "/run/press-node-agent/node.sock"
}
```

### Configuration Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `listen.host` | string | No | TCP bind address for the proxy. Default: `0.0.0.0` |
| `listen.port` | int | No | TCP port for the proxy. Default: `8080` |
| `jwks.url` | string | **Yes** | URL to fetch JWKS (Ed25519 public keys) from |
| `jwks.cache_ttl_seconds` | int | No | How long to cache JWKS keys. Default: `300` |
| `jwt.issuer` | string | **Yes** | Expected `iss` claim in incoming JWTs |
| `jwt.audience` | string | **Yes** | Expected `aud` claim in incoming JWTs |
| `allow_http_agents` | bool | No | Allow agents to register via `.http` files. Default: `false` |
| `controlplanes` | array | No | List of controlplane services to route to |
| `controlplanes[].name` | string | **Yes** | Unique name for this controlplane |
| `controlplanes[].url` | string | **Yes** | Base URL of the controlplane |
| `controlplanes[].prefixes` | array | **Yes** | URL prefixes that route to this controlplane |
| `controlplanes[].jwt_token` | string | **Yes** | JWT used for node-to-controlplane auth |
| `authz.url` | string | No | URL of the authorization service. If empty, authz is disabled |
| `authz.jwt_token` | string | No | JWT used for node-to-authz auth |
| `socket_dir` | string | No | Directory where agent `.sock`/`.http` files are placed. Default: `/run/press-node-agent` |
| `admin_socket` | string | No | Path to the admin Unix domain socket. Default: `<socket_dir>/node.sock` |

## Pythonic Setup

### 1. Install

```bash
pip install .
```

Or install in editable mode for development:

```bash
pip install -e .
```

### 2. Prepare the Socket Directory

```bash
mkdir -p /run/press-node-agent
```

### 3. Create Configuration

Write your `config.json` (see [Configuration File](#configuration-file) above). At minimum, you need `jwks.url`, `jwt.issuer`, and `jwt.audience`.

### 4. Run

```bash
NODE_AGENT_CONFIG=./config.json node-agent
```

Or run directly as a module:

```bash
NODE_AGENT_CONFIG=./config.json python -m node_agent
```

### 5. Verify

```bash
curl http://127.0.0.1:8080/_meta/health
# {"status":"ok"}
```

### Development with Auth Server

For local development, use the bundled auth server that provides JWKS and a permissive authz endpoint:

```bash
# Terminal 1: Start the dev auth server
python development/authz_server.py serve --port 9100

# Terminal 2: Mint a JWT for testing
python development/authz_server.py mint --sub alice --roles admin

# Terminal 3: Start node-agent with the dev config
NODE_AGENT_CONFIG=./config.json node-agent

# Terminal 4: Test with the minted JWT
TOKEN=$(python development/authz_server.py mint --sub alice --roles admin)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/api/v1/cluster
```

### Running Your Own Agent

See [Writing an Agent](4-write-agent.md) for how to create and register an agent with Node Agent.

## Docker Setup

### 1. Build the Image

```bash
docker build -t node-agent .
```

### 2. Run the Container

```bash
docker run -d \
  --name node-agent \
  -p 8080:8080 \
  -v /run/press-node-agent:/run/press-node-agent \
  -v ./config.json:/etc/node-agent/config.json:ro \
  -e LOG_LEVEL=INFO \
  node-agent
```

### Volume Mounts

| Mount | Purpose |
|---|---|
| `/run/press-node-agent` | Shared socket directory. Agents on the host (or sibling containers) place their `.sock` files here for discovery. |
| `./config.json:/etc/node-agent/config.json:ro` | Read-only config file. The container reads from `/etc/node-agent/config.json` by default. |

### Running Agents Alongside

Agents running in separate containers must share the `socket-dir` volume so Node Agent can discover them:

```bash
# Agent container creates a .sock file in the shared volume
docker run -v socket-dir:/run/press-node-agent my-agent
```

For HTTP-mode agents (dev only), set `allow_http_agents: true` in config and have the agent write its `host:port` to `<socket_dir>/<name>.http`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `NODE_AGENT_CONFIG` | `./config.json` | Path to the configuration file |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

## File Layout After Startup

```
/run/press-node-agent/
├── node.sock          # Admin socket (created by node-agent)
├── agent-a.sock       # Agent A's UDS (created by agent)
├── agent-b.sock       # Agent B's UDS (created by agent)
└── agent-c.http       # Agent C's HTTP marker (if allow_http_agents: true)
```

Node Agent watches this directory and automatically registers/deregisters agents as files appear and disappear.
