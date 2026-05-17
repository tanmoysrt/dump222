from __future__ import annotations

from typing import Any


class AuthContext:
    """Parsed auth context from Node Agent headers.

    Attributes:
        source: "external" or "local". Missing means the request bypassed Node Agent.
        sub: User subject from JWT sub claim (external only).
        roles: Comma-separated roles from JWT (external only).
        jti: Token ID from JWT jti claim (external only).
    """

    __slots__ = ("source", "sub", "roles", "jti")

    def __init__(
        self,
        source: str,
        sub: str = "",
        roles: str = "",
        jti: str = "",
    ) -> None:
        self.source = source
        self.sub = sub
        self.roles = roles
        self.jti = jti

    @classmethod
    def from_headers(cls, headers: dict[str, str] | Any) -> AuthContext:
        """Build AuthContext from request headers.

        Accepts a plain dict or a FastAPI/starlette Headers object.
        Returns an instance with source="" if X-Auth-Source is missing.
        """
        if hasattr(headers, "get"):
            get = headers.get
        else:
            get = dict(headers).get

        source = get("x-auth-source", "") or ""
        return cls(
            source=source.lower(),
            sub=get("x-auth-sub", "") or "",
            roles=get("x-auth-roles", "") or "",
            jti=get("x-auth-jti", "") or "",
        )

    @property
    def is_local(self) -> bool:
        return self.source == "local"

    @property
    def is_external(self) -> bool:
        return self.source == "external"

    @property
    def role_list(self) -> list[str]:
        if not self.roles:
            return []
        return [r.strip() for r in self.roles.split(",") if r.strip()]

    def has_role(self, *roles: str) -> bool:
        return bool(set(roles) & set(self.role_list))
