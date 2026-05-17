# Running with Docker

## Build

```bash
docker build -t node-agent .
```

## Run

```bash
docker run \
  -p 8080:8080 \
  -v /run/press-node-agent:/run/press-node-agent \
  -v /path/to/config.json:/etc/node-agent/config.json:ro \
  node-agent
```

`/run/press-node-agent` must be bind-mounted so agents on the host (or in other
containers with the same mount) can drop their `.sock` files and be discovered.

## Environment variables

| Variable            | Default                       | Notes                |
| ------------------- | ----------------------------- | -------------------- |
| `NODE_AGENT_CONFIG` | `/etc/node-agent/config.json` | Path to config file  |
| `LOG_LEVEL`         | `INFO`                        | Python logging level |

## With other containers

Mount the same socket directory into each agent container at the same path:

```bash
docker run \
  -v /run/press-node-agent:/run/press-node-agent \
  my-compute-agent
```

The agent drops `/run/press-node-agent/compute.sock` on startup; node-agent picks
it up via inotify within seconds.
