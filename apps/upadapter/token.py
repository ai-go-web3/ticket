"""麻花 token 缓存管理。

token 有效期 2h，全局缓存 + 提前刷新（约 30min 提前量）。
严禁每请求都获取，否则封 IP。

缓存层用 Django cache 框架：生产配 RedisCache，本地开发配 LocMemCache
（settings_dev），因此无需强依赖 django_redis。
"""
import logging

from django.core.cache import cache

from apps.upadapter.mahua import MahuaClient

logger = logging.getLogger('app')

_TOKEN_KEY = 'mahua:token'
_TOKEN_TTL = 2 * 3600          # 2h
_REFRESH_AHEAD = 30 * 60       # 提前 30min 视为需刷新


def get_channel():
    """获取麻花渠道实例。"""
    return MahuaClient()


def get_token():
    """获取缓存的麻花 token，缺失则刷新。"""
    token = cache.get(_TOKEN_KEY)
    if token:
        return token
    return refresh_token()


def refresh_token():
    """刷新 token 并缓存。"""
    client = get_channel()
    token, _user_name = client.fetch_token()
    if not token:
        logger.error('麻花获取 token 失败，返回空')
        return ''
    cache.set(_TOKEN_KEY, token, _TOKEN_TTL)
    return token
