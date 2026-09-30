"""微信支付服务。

统一下单 -> 返回小程序 requestPayment 参数；
回调 -> 验签 -> 幂等 -> 触发订单出票。

说明：正式场景复用公司支付中心封装；骨架阶段按微信支付 v2 统一下单协议实现，
并留出接入公司支付中心的抽象点。
"""
import hashlib
import logging
import time
import uuid
import xml.etree.ElementTree as ET

import requests
from django.conf import settings

from apps.common.response import BizError
from apps.order.models import TicketOrder

logger = logging.getLogger('app')


def _sign(params, key):
    """微信支付 MD5 签名（v2）。"""
    s = '&'.join(f'{k}={params[k]}' for k in sorted(params) if params[k] not in ('', None))
    s += f'&key={key}'
    return hashlib.md5(s.encode()).hexdigest().upper()


def _dict_to_xml(params):
    parts = ['<xml>']
    for k, v in params.items():
        parts.append(f'<{k}>{v}</{k}>')
    parts.append('</xml>')
    return ''.join(parts)


def _xml_to_dict(xml_str):
    root = ET.fromstring(xml_str)
    return {child.tag: child.text for child in root}


def unified_order(order_id, openid):
    """统一下单，返回 requestPayment 参数。

    开发降级：未配置 WX_MCHID/WX_PAY_KEY 时，返回「模拟支付」参数
    （前端 detect 到 mock 标记后走本地模拟支付成功，便于端到端联调）。
    """
    order = TicketOrder.objects.get(id=order_id)
    cfg = settings.WECHAT

    if not (cfg.get('MCHID') and cfg.get('PAY_KEY')):
        logger.warning('WX_MCHID/WX_PAY_KEY 未配置，返回模拟支付参数')
        return {
            'mock': True,
            'orderId': order_id,
            'orderExtNo': order.order_ext_no,
            'payAmount': order.pay_amount,
            'timeStamp': str(int(time.time())),
            'nonceStr': uuid.uuid4().hex,
            'package': 'mock_prepay_id',
            'signType': 'MD5',
            'paySign': 'MOCK',
        }

    out_trade_no = order.order_ext_no
    params = {
        'appid': cfg['APPID'],
        'mch_id': cfg['MCHID'],
        'nonce_str': uuid.uuid4().hex,
        'body': '电影票',
        'out_trade_no': out_trade_no,
        'total_fee': order.pay_amount,   # 单位：分
        'spbill_create_ip': '127.0.0.1',
        'notify_url': cfg['NOTIFY_URL'],
        'trade_type': 'JSAPI',
        'openid': openid,
    }
    params['sign'] = _sign(params, cfg['PAY_KEY'])

    resp = requests.post(
        'https://api.mch.weixin.qq.com/pay/unifiedorder',
        data=_dict_to_xml(params).encode(),
        timeout=10,
    )
    result = _xml_to_dict(resp.text)
    if result.get('return_code') != 'SUCCESS' or result.get('result_code') != 'SUCCESS':
        logger.error('unified order failed: %s', result)
        raise BizError(f"统一下单失败: {result.get('return_msg') or result.get('err_code_des')}")

    prepay_id = result['prepay_id']
    # 构造 requestPayment 参数（二次签名）
    pay_params = {
        'appId': cfg['APPID'],
        'timeStamp': str(int(time.time())),
        'nonceStr': uuid.uuid4().hex,
        'package': f'prepay_id={prepay_id}',
        'signType': 'MD5',
    }
    pay_params['paySign'] = _sign(pay_params, cfg['PAY_KEY'])
    return pay_params


def mock_pay_success(order_id, user_id=None):
    """开发降级：模拟支付成功，走完「支付→出票」闭环。

    仅当未配置真实商户号（WX_MCHID/WX_PAY_KEY 为空）时可用，避免联调期无支付通道阻塞。
    """
    cfg = settings.WECHAT
    if cfg.get('MCHID') and cfg.get('PAY_KEY'):
        raise BizError('已配置真实支付，不允许模拟支付')

    try:
        order = TicketOrder.objects.get(id=order_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)
    if user_id is not None and order.user_id != user_id:
        raise BizError('无权操作该订单', code=ErrorCode.FORBIDDEN)

    from apps.order.services import mark_paid
    pay_no = f'MOCK{order.order_ext_no}'
    mark_paid(order.id, pay_no, order.pay_amount, callback_raw={'mock': True})
    return order


def on_pay_callback(request):
    """支付回调（微信异步通知）。"""
    from apps.order.services import mark_paid
    from apps.order.models import OrderPayment

    result = _xml_to_dict(request.body)
    if result.get('return_code') != 'SUCCESS':
        return {'return_code': 'FAIL'}

    # 验签（骨架阶段略过完整验签，标注 TODO）
    pay_no = result.get('transaction_id')
    order_ext_no = result.get('out_trade_no')
    amount = int(result.get('total_fee', 0))

    try:
        order = TicketOrder.objects.get(order_ext_no=order_ext_no)
    except TicketOrder.DoesNotExist:
        return {'return_code': 'FAIL'}

    mark_paid(order.id, pay_no, amount, callback_raw=result)
    return {'return_code': 'SUCCESS'}
