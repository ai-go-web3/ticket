"""微信小程序服务：code2Session / getPhoneNumber。"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger('app')


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

    url = 'https://api.weixin.qq.com/sns/jscode2session'
    resp = requests.get(url, params={
        'appid': appid,
        'secret': secret,
        'js_code': code,
        'grant_type': 'authorization_code',
    }, timeout=5)
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

    url = 'https://api.weixin.qq.com/cgi-bin/token'
    resp = requests.get(url, params={
        'grant_type': 'client_credential',
        'appid': appid,
        'secret': secret,
    }, timeout=5)
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
    url = 'https://api.weixin.qq.com/wxa/business/getuserphonenumber'
    resp = requests.post(
        url,
        params={'access_token': access_token},
        json={'code': code},
        timeout=5,
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
