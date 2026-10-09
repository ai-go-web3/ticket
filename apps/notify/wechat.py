"""微信订阅消息发送：stable access_token 管理 + subscribe/send。

复用 apps.auths.wechat._wx_api（含云托管内网 HTTPS 证书自签降级），避免两处逻辑漂移。
token 用 getStableAccessToken（POST /cgi-bin/stable_token）——相比旧 /cgi-bin/token
不会与其他进程互相顶掉，缓存进 Django cache（进程内 LocMem），提前 5 分钟过期。
发送时遇 40001/42001（token 失效）强制刷新重试一次。
"""
import logging

from django.conf import settings
from django.core.cache import cache

# 复用登录模块的微信 HTTP 封装（SSL 降级策略集中一处）
from apps.auths.wechat import _wx_api

logger = logging.getLogger('app')

_TOKEN_CACHE_KEY = 'wx_stable_access_token'


def _configured():
    """是否配置了小程序 APPID/SECRET（未配置则通知链路整体降级为不发）。"""
    return bool(settings.WECHAT.get('APPID') and settings.WECHAT.get('SECRET'))


def get_stable_access_token(force_refresh=False):
    """获取 stable access_token（带缓存）。未配置凭证时抛错，由调用方按「发不出」处理。"""
    if not _configured():
        raise ValueError('WX_APPID/WX_SECRET 未配置，无法发送订阅消息')

    if not force_refresh:
        token = cache.get(_TOKEN_CACHE_KEY)
        if token:
            return token

    resp = _wx_api('POST', '/cgi-bin/stable_token', json={
        'grant_type': 'client_credential',
        'appid': settings.WECHAT['APPID'],
        'secret': settings.WECHAT['SECRET'],
        'force_refresh': bool(force_refresh),
    })
    data = resp.json()
    token = data.get('access_token')
    if not token:
        logger.error('获取 stable access_token 失败: %s', data)
        raise ValueError(f"获取 access_token 失败: {data.get('errmsg', '未知错误')}")
    cache.set(_TOKEN_CACHE_KEY, token, timeout=max(0, int(data.get('expires_in', 7200)) - 300))
    return token


def send_subscribe(openid, template_id, page, data):
    """发送一次性订阅消息。返回微信响应 dict（含 errcode/errmsg）。

    errcode==0 成功；40001/42001（token 失效）自动强刷重试一次；
    调用方需保证 openid/template_id/data 合法。凭证未配置时直接返回错误 dict（不抛）。
    """
    if not _configured():
        return {'errcode': -1, 'errmsg': 'WX_APPID/SECRET 未配置'}
    if not openid or not template_id:
        return {'errcode': -1, 'errmsg': 'openid 或 template_id 为空'}

    payload = {
        'touser': openid,
        'template_id': template_id,
        'page': page,
        'data': data,
        'miniprogram_state': getattr(settings, 'WX_SUBSCRIBE_STATE', 'formal'),
        'lang': 'zh_CN',
    }

    def _post(token):
        resp = _wx_api('POST', '/cgi-bin/message/subscribe/send',
                       params={'access_token': token}, json=payload)
        return resp.json()

    try:
        result = _post(get_stable_access_token())
    except ValueError as exc:  # 获取 token 失败（未配置等）
        return {'errcode': -1, 'errmsg': str(exc)}

    if result.get('errcode') in (40001, 42001):
        logger.info('订阅消息 token 失效，强刷后重试一次')
        try:
            result = _post(get_stable_access_token(force_refresh=True))
        except ValueError as exc:
            return {'errcode': -1, 'errmsg': str(exc)}
    return result
