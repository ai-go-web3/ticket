"""后台管理鉴权。

独立于 C 端 apps.auths.authentication：同样用 HMAC-SHA256 简易 JWT，但 payload 带
role=admin + aid(AdminUser.id)，与 C 端 token(uid) 互不通用。

⚠ 后台接口必须用 @authentication_classes([AdminJWTAuthentication]) 显式覆盖，
   否则全局默认的 apps.auths.authentication.JWTAuthentication 会拿 aid 去查 AppUser，
   查不到直接 401（详见 docs/B端运营后台落地方案.md §3）。
"""
import base64
import hashlib
import hmac
import json
import time

from django.conf import settings
from rest_framework import exceptions
from rest_framework.authentication import BaseAuthentication

from apps.adminapi.models import AdminUser

ADMIN_TOKEN_EXPIRE_SECONDS = int(getattr(settings, 'ADMIN_TOKEN_EXPIRE_SECONDS', 7200) or 7200)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _b64url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def gen_admin_token(admin_id: int, expire_seconds: int = None) -> str:
    """签发管理 token（role=admin）。"""
    expire_seconds = expire_seconds or ADMIN_TOKEN_EXPIRE_SECONDS
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {'aid': admin_id, 'role': 'admin', 'exp': int(time.time()) + expire_seconds}
    header_b64 = _b64url_encode(json.dumps(header).encode())
    payload_b64 = _b64url_encode(json.dumps(payload).encode())
    sig = _sign(f'{header_b64}.{payload_b64}', settings.SECRET_KEY)
    return f'{header_b64}.{payload_b64}.{sig}'


def decode_admin_token(token: str) -> dict:
    """校验并解析管理 token，返回 payload；失败抛 AuthenticationFailed。"""
    try:
        header_b64, payload_b64, sig = token.split('.')
    except ValueError:
        raise exceptions.AuthenticationFailed('token 格式错误')
    if not hmac.compare_digest(_sign(f'{header_b64}.{payload_b64}', settings.SECRET_KEY), sig):
        raise exceptions.AuthenticationFailed('token 签名无效')
    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get('role') != 'admin':
        raise exceptions.AuthenticationFailed('非管理令牌')
    if payload.get('exp', 0) < time.time():
        raise exceptions.AuthenticationFailed('登录已过期')
    return payload


class AdminJWTAuthentication(BaseAuthentication):
    """管理后台鉴权：解析 role=admin token -> request.admin。"""

    keyword = 'Bearer'

    def authenticate(self, request):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith(f'{self.keyword} '):
            return None
        payload = decode_admin_token(auth[len(self.keyword):].strip())
        admin = AdminUser.objects.filter(id=payload.get('aid'), is_active=1).first()
        if not admin:
            raise exceptions.AuthenticationFailed('管理员不存在或已停用')
        request.admin = admin
        request.admin_id = admin.id
        return (admin, payload)

    def authenticate_header(self, request):
        return self.keyword
