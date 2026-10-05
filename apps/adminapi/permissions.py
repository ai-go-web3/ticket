"""后台权限类。"""
from rest_framework.permissions import BasePermission

from apps.adminapi.models import AdminUser


class IsAdmin(BasePermission):
    """要求请求已通过 AdminJWTAuthentication（request.admin 为 AdminUser）。"""

    def has_permission(self, request, view):
        admin = getattr(request, 'admin', None)
        return isinstance(admin, AdminUser) and admin.is_active == 1
