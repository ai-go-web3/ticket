"""麻花 token 缓存管理。

token 有效期 2h，全局缓存 + 内置定时器每 30 分钟主动刷新一次（见 apps.catalog.scheduler），
保证任何时刻取到的都是新鲜 token，且严禁每请求都登录（否则封 IP）。

所有调麻花的接口都应通过 get_token() 取 token，不要各自 fetch_token()。

缓存层用 Django cache 框架，后端为进程内 LocMemCache（全项目已去 Redis）。
LocMem 为单进程内存，多进程/多副本间不共享，刷新锁只能在本进程内去重；
建议单副本常驻，或接受各进程独立刷新 token（登录频率仍远低于「每请求」）。
"""
import logging
import time

from django.core.cache import cache

from apps.upadapter.mahua import MahuaClient

logger = logging.getLogger('app')

_TOKEN_KEY = 'mahua:token'
_LOCK_KEY = 'mahua:token:refresh_lock'
_TOKEN_TTL = 2 * 3600          # 缓存 2h（定时器每 30min 会覆盖刷新，此为兜底存活期）


def get_channel():
    """获取麻花渠道实例。"""
    return MahuaClient()


def get_token():
    """获取缓存的麻花 token；缺失则刷新（冷启动/兜底路径）。"""
    token = cache.get(_TOKEN_KEY)
    if token:
        return token
    return refresh_token()


def refresh_token():
    """强制登录刷新 token 并缓存；带锁防并发/多副本重复登录。

    定时器每 30 分钟调用一次（scheduled_refresh）。抢不到锁的进程会短暂等待
    读取他处刷好的结果，避免同一时刻多次登录麻花。刷新失败时保留旧 token。
    """
    got_lock = cache.add(_LOCK_KEY, '1', timeout=15)
    try:
        if not got_lock:
            # 其它进程正在刷新：最多等 ~3s 读其结果
            for _ in range(15):
                time.sleep(0.2)
                token = cache.get(_TOKEN_KEY)
                if token:
                    return token

        client = get_channel()
        token, _user_name = client.fetch_token()
        if not token:
            old = cache.get(_TOKEN_KEY) or ''
            logger.error('麻花刷新 token 失败，%s', '保留旧 token' if old else '无可用 token')
            return old

        cache.set(_TOKEN_KEY, token, _TOKEN_TTL)
        logger.info('麻花 token 已刷新并缓存（TTL %ds）', _TOKEN_TTL)
        return token
    finally:
        if got_lock:
            cache.delete(_LOCK_KEY)


def scheduled_refresh():
    """供内置定时器调用的刷新入口。返回刷新后的 token（或空串）。"""
    return refresh_token()
