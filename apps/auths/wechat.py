"""微信小程序服务：code2Session / getPhoneNumber。"""
import logging
import os

import requests
from django.conf import settings

logger = logging.getLogger('app')

_WX_HTTPS = 'https://api.weixin.qq.com'
_WX_HTTP = 'http://api.weixin.qq.com'  # 云托管内网链路（开放接口服务会接管该域名的 HTTPS）


def _wx_api(method, path, **kwargs):
    """请求微信开放接口（api.weixin.qq.com），带 SSL 降级。

    背景：微信云托管开启「开放接口服务/云调用」后，容器内对该域名的 HTTPS 请求
    会被平台旁挂组件接管并出示自签证书；Python requests 默认用 certifi 信任库，
    不认该证书，报 CERTIFICATE_VERIFY_FAILED(self-signed certificate)。

    处理策略（与 Dockerfile entrypoint.sh 配合）：
      1. 首选 HTTPS 严格校验（entrypoint.sh 已把 REQUESTS_CA_BUNDLE 指向系统
         信任库，平台自签证书已合入其中，正常情况下直接通过）；
      2. 若仍报 SSLError，自动降级为 HTTP 内网链路重试（云托管容器与微信 API
         同内网，明文可接受；本地环境一般不会触发此分支）；
      3. 设 WX_HTTP_STRICT_SSL=true 可强制严格模式（禁止降级）。
    """
    kwargs.setdefault('timeout', 5)
    strict = os.environ.get('WX_HTTP_STRICT_SSL', '').strip().lower() in ('1', 'true', 'yes')
    try:
        return requests.request(method, f'{_WX_HTTPS}{path}', **kwargs)
    except requests.exceptions.SSLError as e:
        if strict:
            raise
        logger.warning('微信接口 HTTPS 证书校验失败（疑似云托管旁挂组件接管），降级 HTTP 重试: %s', e)
        return requests.request(method, f'{_WX_HTTP}{path}', **kwargs)


def _is_dev_fallback():
    """是否走开发降级（未配置 APPID/SECRET 时本地联调用）。"""
    return not (settings.WECHAT.get('APPID') and settings.WECHAT.get('SECRET'))


def code2session(code):
    """wx.login code 换 openid/session_key。

    正式环境：调微信 jscode2session。
    开发降级：未配置 APPID/SECRET 时，用 `dev_<code>` 模拟 openid，便于本地联调。
    """
    appid = settings.WECHAT.get('APPID', '')
    secret = settings.WECHAT.get('SECRET', '')

    if not appid or not secret:
        logger.warning('WX_APPID/WX_SECRET 未配置，走开发降级（模拟 openid）')
        return {
            'openid': f'dev_{code}',
            'session_key': 'dev_session_key',
            'unionid': None,
        }

    url_path = '/sns/jscode2session'
    resp = _wx_api('GET', url_path, params={
        'appid': appid,
        'secret': secret,
        'js_code': code,
        'grant_type': 'authorization_code',
    })
    data = resp.json()
    if 'openid' not in data:
        logger.error('code2session failed: %s', data)
        raise ValueError(f"微信登录失败: {data.get('errmsg', '未知错误')}")
    return data


def _get_access_token():
    """获取小程序全局 access_token（带缓存，7200s 有效）。"""
    appid = settings.WECHAT.get('APPID', '')
    secret = settings.WECHAT.get('SECRET', '')
    if not appid or not secret:
        raise ValueError('WX_APPID/WX_SECRET 未配置，无法获取 access_token')

    cache = __import__('django.core.cache', fromlist=['cache']).cache
    key = 'wx_access_token'
    token = cache.get(key)
    if token:
        return token

    resp = _wx_api('GET', '/cgi-bin/token', params={
        'grant_type': 'client_credential',
        'appid': appid,
        'secret': secret,
    })
    data = resp.json()
    if 'access_token' not in data:
        logger.error('get access_token failed: %s', data)
        raise ValueError(f"获取 access_token 失败: {data.get('errmsg', '未知错误')}")
    token = data['access_token']
    # 提前 5 分钟过期，避免边界
    cache.set(key, token, timeout=max(0, data.get('expires_in', 7200) - 300))
    return token


def get_phone(code):
    """getPhoneNumber code 换真实手机号。

    调 /wxa/business/getuserphonenumber，需 access_token + 用户授权 code。
    开发降级：未配置 APPID/SECRET 时抛错（手机号必须真实环境才有）。
    """
    if _is_dev_fallback():
        raise ValueError('未配置 WX_APPID/WX_SECRET，无法换取真实手机号')

    access_token = _get_access_token()
    resp = _wx_api(
        'POST',
        '/wxa/business/getuserphonenumber',
        params={'access_token': access_token},
        json={'code': code},
    )
    data = resp.json()
    if data.get('errcode') != 0:
        logger.error('getuserphonenumber failed: %s', data)
        raise ValueError(f"获取手机号失败: {data.get('errmsg', '未知错误')}")

    phone_info = data.get('phone_info', {})
    return {
        'phone': phone_info.get('purePhoneNumber') or phone_info.get('phoneNumber', ''),
        'countryCode': phone_info.get('countryCode', '86'),
    }
