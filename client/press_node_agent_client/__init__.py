"""Press Node Agent client library.

Public API:
    get_node_agent_socket()         - Path to node.sock
    get_my_agent_socket_path()      - Where to create your agent's .sock
    send_request_async()            - Async proxy request
    send_request_sync()             - Sync proxy request
    has_permission_async()          - Async authz check
    has_permission_sync()           - Sync authz check
    refresh_agent_routes_async()    - Async route refresh
    refresh_agent_routes_sync()     - Sync route refresh
    AuthContext                     - Parse auth headers into a typed object
"""

from .auth_context import AuthContext
from .client_async import (
    has_permission as has_permission_async,
    refresh_agent_routes as refresh_agent_routes_async,
    send_request as send_request_async,
)
from .client_sync import (
    has_permission as has_permission_sync,
    refresh_agent_routes as refresh_agent_routes_sync,
    send_request as send_request_sync,
)
from .config import get_my_agent_socket_path, get_node_agent_socket

__all__ = [
    "AuthContext",
    "get_node_agent_socket",
    "get_my_agent_socket_path",
    "send_request_async",
    "send_request_sync",
    "has_permission_async",
    "has_permission_sync",
    "refresh_agent_routes_async",
    "refresh_agent_routes_sync",
]
