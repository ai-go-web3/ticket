"""JWT 鉴权。

小程序端 wx.login 换 code -> 后端调微信 code2Session 换 openid -> 签发内部 JWT。
后续请求携带 Authorization: Bearer <token>，本类校验并注入 request.user。
"""
import time
import hashlib
import hmac
import base64
import json

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework import exceptions

from apps.auths.models import AppUser


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def _b64url_decode(data: str) -> bytes:
    padding = '=' * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()


def gen_token(user_id: int, expire_seconds: int = None) -> str:
    """签发内部 JWT。"""
    expire_seconds = expire_seconds or getattr(settings, 'JWT_EXPIRE_SECONDS', 7200)
    header = {'alg': 'HS256', 'typ': 'JWT'}
    payload = {'uid': user_id, 'exp': int(time.time()) + expire_seconds}
    header_b64 = _b64url_encode(json.dumps(header).encode())
    payload_b64 = _b64url_encode(json.dumps(payload).encode())
    sig = _sign(f'{header_b64}.{payload_b64}', settings.SECRET_KEY)
    return f'{header_b64}.{payload_b64}.{sig}'


def decode_token(token: str):
    """校验并解析 JWT，返回 payload 或抛异常。"""
    try:
        header_b64, payload_b64, sig = token.split('.')
    except ValueError:
        raise exceptions.AuthenticationFailed('token 格式错误')
    if not hmac.compare_digest(_sign(f'{header_b64}.{payload_b64}', settings.SECRET_KEY), sig):
        raise exceptions.AuthenticationFailed('token 签名无效')
    payload = json.loads(_b64url_decode(payload_b64))
    if payload.get('exp', 0) < time.time():
        raise exceptions.AuthenticationFailed('token 已过期')
    return payload


class JWTAuthentication(BaseAuthentication):
    """JWT 鉴权类。"""

    def authenticate(self, request):
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        token = auth[7:].strip()
        try:
            payload = decode_token(token)
        except exceptions.AuthenticationFailed:
            raise exceptions.AuthenticationFailed('未登录或登录已失效')
        user_id = payload.get('uid')
        try:
            user = AppUser.objects.get(id=user_id, deleted=0)
        except AppUser.DoesNotExist:
            raise exceptions.AuthenticationFailed('用户不存在')
        request.user_id = user.id
        return (user, token)
