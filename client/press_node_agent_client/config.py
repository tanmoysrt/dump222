from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SOCKET_DIR = "/run/press-node-agent"
NODE_AGENT_SOCK_ENV = "NODE_AGENT_SOCKET"
AGENT_SOCKET_DIR_ENV = "AGENT_SOCKET_DIR"


def get_node_agent_socket() -> str:
    """Return the path to the node-agent admin socket.

    Resolves NODE_AGENT_SOCKET env variable first, then falls back to
    <AGENT_SOCKET_DIR>/node.sock, and finally /run/press-node-agent/node.sock.
    """
    sock = os.environ.get(NODE_AGENT_SOCK_ENV)
    if sock:
        return sock

    socket_dir = os.environ.get(AGENT_SOCKET_DIR_ENV, DEFAULT_SOCKET_DIR)
    return str(Path(socket_dir) / "node.sock")


def get_my_agent_socket_path(agent_name: str) -> str:
    """Return the path where this agent should create its socket file.

    Uses AGENT_SOCKET_DIR env variable, falling back to
    /run/press-node-agent. The filename is <agent_name>.sock.
    """
    socket_dir = os.environ.get(AGENT_SOCKET_DIR_ENV, DEFAULT_SOCKET_DIR)
    return str(Path(socket_dir) / f"{agent_name}.sock")
