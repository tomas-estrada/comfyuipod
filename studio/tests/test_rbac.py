"""Unit tests para RBAC."""

from studio.comfy_studio.models.rbac import Permission, Role


def get_permissions(user_roles: list[Role]):
    """Mapea roles a permisos."""
    permissions = set()
    for role in user_roles:
        if role == Role.ADMIN:
            permissions.update([
                Permission.READ, Permission.WRITE, 
                Permission.DELETE, Permission.EXECUTE
            ])
        elif role == Role.EDITOR:
            permissions.update([Permission.READ, Permission.WRITE])
        elif role == Role.VIEWER:
            permissions.update([Permission.READ])
    return permissions


def test_permissions():
    """Prueba que los permisos se mapeen correctamente."""
    assert Permission.WRITE in get_permissions([Role.ADMIN])
    assert Permission.WRITE not in get_permissions([Role.VIEWER])
    assert Permission.READ in get_permissions([Role.VIEWER])
    assert Permission.EXECUTE in get_permissions([Role.ADMIN])
    assert Permission.EXECUTE not in get_permissions([Role.EDITOR])