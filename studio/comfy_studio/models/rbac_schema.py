"""Esquema de base de datos para RBAC usando SQLAlchemy."""

from sqlalchemy import Column, Enum, ForeignKey, String, Table
from sqlalchemy.orm import relationship
from studio.comfy_studio.database import Base
from studio.comfy_studio.models.rbac import Permission, Role

# Tabla de asociación para usuarios y roles
user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String, ForeignKey("users.id")),
    Column("role", Enum(Role), ForeignKey("roles.name")),
)


class RoleModel(Base):
    """Modelo de roles en la base de datos."""
    __tablename__ = "roles"
    name = Column(Enum(Role), primary_key=True)
    permissions = relationship("PermissionModel", back_populates="role")


class PermissionModel(Base):
    """Modelo de permisos en la base de datos."""
    __tablename__ = "permissions"
    id = Column(String, primary_key=True)
    role_name = Column(Enum(Role), ForeignKey("roles.name"))
    permission = Column(Enum(Permission))
    role = relationship("RoleModel", back_populates="permissions")