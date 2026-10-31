"""Definición de roles y permisos para RBAC."""

from enum import StrEnum

from pydantic import BaseModel


class Role(StrEnum):
    """Roles disponibles en el sistema."""
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    GUEST = "guest"


class Permission(StrEnum):
    """Permisos disponibles en el sistema."""
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXECUTE = "execute"


class RolePermission(BaseModel):
    """Mapeo de roles a permisos."""
    role: Role
    permissions: list[Permission]