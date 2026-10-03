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

from apps.common.response import BizError, ErrorCode
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
    """支付回调（微信异步通知）：验签 + 金额校验 + 幂等落库。"""
    from apps.order.services import mark_paid

    body = request.body or b''
    logger.info('收到支付回调 body_len=%s', len(body))

    try:
        result = _xml_to_dict(body)
    except Exception as exc:  # noqa: BLE001 非 XML 报文：直接拒绝（微信只发合法 XML）
        logger.error('支付回调报文非法(非XML) err=%s body=%s', exc, body[:512])
        return {'return_code': 'FAIL', 'return_msg': 'invalid xml'}

    pay_no = result.get('transaction_id')
    order_ext_no = result.get('out_trade_no')

    if result.get('return_code') != 'SUCCESS':
        logger.error('支付回调 return_code 非SUCCESS return_code=%s return_msg=%s out_trade_no=%s',
                     result.get('return_code'), result.get('return_msg'), order_ext_no)
        return {'return_code': 'FAIL', 'return_msg': 'invalid notify'}

    logger.info('支付回调已解析 out_trade_no=%s transaction_id=%s result_code=%s total_fee=%s',
                order_ext_no, pay_no, result.get('result_code'), result.get('total_fee'))

    if result.get('result_code') != 'SUCCESS':
        # 支付失败/关单等通知：确认收到即可，不改订单状态
        logger.warning('支付回调非成功通知(不改单) out_trade_no=%s result_code=%s err_code=%s err_code_des=%s',
                       order_ext_no, result.get('result_code'),
                       result.get('err_code'), result.get('err_code_des'))
        return {'return_code': 'SUCCESS'}

    cfg = settings.WECHAT
    if not cfg.get('PAY_KEY'):
        # 未配置 PAY_KEY -> 验签整段跳过、照单全收：降级环境可用，生产严禁，务必留痕。
        logger.warning('支付回调未配置 PAY_KEY，跳过验签直接受理 out_trade_no=%s（仅降级环境，生产严禁）',
                       order_ext_no)
    elif not _verify_sign(dict(result), cfg['PAY_KEY']):
        logger.error('支付回调验签失败 out_trade_no=%s', order_ext_no)
        return {'return_code': 'FAIL', 'return_msg': 'sign error'}

    amount = int(result.get('total_fee', 0))

    try:
        order = TicketOrder.objects.get(order_ext_no=order_ext_no)
    except TicketOrder.DoesNotExist:
        logger.error('支付回调订单不存在 out_trade_no=%s', order_ext_no)
        return {'return_code': 'FAIL', 'return_msg': 'order not found'}

    # 金额校验：回调金额必须与订单应付一致（防篡改/错单）
    if amount != order.pay_amount:
        logger.error('支付回调金额不符 order=%s 回调=%s分 应付=%s分',
                     order_ext_no, amount, order.pay_amount)
        return {'return_code': 'FAIL', 'return_msg': 'amount mismatch'}

    mark_paid(order.id, pay_no, amount, callback_raw=result)
    logger.info('支付回调处理成功 out_trade_no=%s order_id=%s pay_no=%s 金额=%s分',
                order_ext_no, order.id, pay_no, amount)
    return {'return_code': 'SUCCESS'}


def _verify_sign(params, key):
    """微信支付 v2 回调验签：去掉 sign 后按 k=v& 排序拼接 MD5 对比。"""
    sign = params.pop('sign', '')
    return bool(sign) and sign == _sign(params, key)


# ===== 微信退款（放单失败/回调失败场景的用户回款） =====

REFUND_API = 'https://api.mch.weixin.qq.com/secapi/pay/refund'


def wx_refund(order, refund):
    """发起微信退款（v2 /secapi/pay/refund，双向证书）。

    返回 (受理成功?, 微信退款单号或 None)。
    - 已配置商户证书（WX_MCH_CERT_PATH/WX_MCH_KEY_PATH）：真实退款，打回真金白银；
    - 未配置证书：骨架降级（视为受理成功，不真打款），联调环境无证书也能跑通状态链路。
    退款被微信拒绝时抛 BizError，由调用方决定回滚/重试（订单停在「退款中」待补偿）。
    """
    cfg = settings.WECHAT
    if not (cfg.get('MCHID') and cfg.get('PAY_KEY')):
        logger.warning('微信支付未配置，退款降级为骨架标记 refund=%s', refund.refund_ext_no)
        return True, None
    cert, key = cfg.get('MCH_CERT_PATH') or '', cfg.get('MCH_KEY_PATH') or ''
    real_refund = bool(cert and key)
    if not real_refund:
        logger.warning('商户API证书未配置，退款降级为骨架标记 refund=%s', refund.refund_ext_no)

    if refund.refund_amount <= 0:
        raise BizError('退款金额非法')

    params = {
        'appid': cfg['APPID'],
        'mch_id': cfg['MCHID'],
        'nonce_str': uuid.uuid4().hex,
        'out_refund_no': refund.refund_ext_no,
        'out_trade_no': order.order_ext_no,
        'total_fee': int(order.pay_amount),
        'refund_fee': int(refund.refund_amount),
        'op_user_id': cfg['MCHID'],
    }
    # 退款结果通知地址：未显式配置时从支付回调地址推导（/pay/callback -> /pay/refund-notify）
    notify_url = cfg.get('REFUND_NOTIFY_URL') or (cfg.get('NOTIFY_URL') or '').replace(
        '/pay/callback', '/pay/refund-notify')
    if notify_url:
        params['notify_url'] = notify_url
    params['sign'] = _sign(params, cfg['PAY_KEY'])

    resp = requests.post(
        REFUND_API, data=_dict_to_xml(params).encode(),
        cert=(cert, key) if real_refund else None, timeout=15,
    )
    result = _xml_to_dict(resp.text)
    if result.get('return_code') != 'SUCCESS':
        logger.error('微信退款请求失败 refund=%s result=%s', refund.refund_ext_no, result)
        raise BizError(f"微信退款失败: {result.get('return_msg') or '通信异常'}")
    if result.get('result_code') != 'SUCCESS':
        logger.error('微信退款被拒 refund=%s err_code=%s des=%s',
                     refund.refund_ext_no, result.get('err_code'), result.get('err_code_des'))
        raise BizError(f"微信退款失败: {result.get('err_code_des') or result.get('err_code')}")

    wx_refund_no = result.get('refund_id') or ''
    logger.info('微信退款已受理 refund=%s wx_refund_no=%s 金额=%s分',
                refund.refund_ext_no, wx_refund_no, refund.refund_amount)
    return True, wx_refund_no


def _decrypt_req_info(req_info_b64, pay_key):
    """解密退款通知 req_info：base64 -> AES-256-ECB(key=md5(pay_key)) -> PKCS7 去填充。"""
    import base64
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = hashlib.md5(pay_key.encode()).hexdigest().lower().encode()
    data = base64.b64decode(req_info_b64)
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    padded = dec.update(data) + dec.finalize()
    return padded[:-padded[-1]]  # PKCS7 去填充


def _reconcile_order_on_refund(refund, arrived):
    """退款通知后的订单状态对账（幂等）。

    - 到账：确保订单为「已退款(80)」且支付状态已回冲（受理时已置位的跳过）；
    - 失败：若订单已被受理时乐观置为「已退款」，回退为「退款中(70)」，
      退款单记 FAIL，由退款重试任务（retry_failed_refunds）重新发起。
    """
    from apps.order.statemachine import transition
    from apps.refund.models import Refund

    order = TicketOrder.objects.filter(id=refund.order_id).first()
    if not order:
        return
    if arrived:
        if order.status == TicketOrder.STATUS_REFUNDING:
            transition(order, TicketOrder.STATUS_REFUNDED)
        if order.pay_status != TicketOrder.PAY_REFUNDED:
            TicketOrder.objects.filter(id=order.id).update(
                pay_status=TicketOrder.PAY_REFUNDED)
    else:
        if order.status == TicketOrder.STATUS_REFUNDED:
            # 已退款是终态，回退需 force；受理时的乐观置位被真实结果推翻
            transition(order, TicketOrder.STATUS_REFUNDING, force=True)
            TicketOrder.objects.filter(id=order.id).update(
                pay_status=TicketOrder.PAY_DONE)
        refund.status = Refund.STATUS_FAIL
        refund.save(update_fields=['status', 'updated_at'])


def on_refund_notify(request):
    """退款结果通知（微信异步推送）：解密 req_info，回写退款单状态。

    refund_status: SUCCESS/CHANGE(退款异常退回用户卡) -> 到账；FAIL/REFUNDCLOSE -> 失败。
    返回 {'return_code': ...} 供视图层应答微信。
    """
    from apps.refund.models import Refund

    body = request.body or b''
    logger.info('收到退款通知 body_len=%s', len(body))

    try:
        result = _xml_to_dict(body)
    except Exception as exc:  # noqa: BLE001 非 XML 报文：拒绝并留痕
        logger.error('退款通知报文非法(非XML) err=%s body=%s', exc, body[:512])
        return {'return_code': 'FAIL', 'return_msg': 'invalid notify'}

    if result.get('return_code') != 'SUCCESS' or not result.get('req_info'):
        logger.error('退款通知无效 return_code=%s has_req_info=%s',
                     result.get('return_code'), bool(result.get('req_info')))
        return {'return_code': 'FAIL', 'return_msg': 'invalid notify'}

    pay_key = settings.WECHAT.get('PAY_KEY') or ''
    if not pay_key:
        logger.error('退款通知无法处理：未配置 PAY_KEY，无法解密 req_info')
        return {'return_code': 'FAIL', 'return_msg': 'pay key missing'}
    try:
        plain = _decrypt_req_info(result['req_info'], pay_key)
        info = _xml_to_dict(plain.decode('utf-8'))
    except Exception as exc:  # noqa: BLE001 解密失败多为密钥不匹配
        logger.error('退款通知解密失败 err=%s', exc)
        return {'return_code': 'FAIL', 'return_msg': 'decrypt error'}

    out_refund_no = info.get('out_refund_no') or ''
    refund_status = info.get('refund_status') or ''
    refund = Refund.objects.filter(refund_ext_no=out_refund_no).first()
    if not refund:
        logger.error('退款通知找不到退款单 out_refund_no=%s', out_refund_no)
        return {'return_code': 'SUCCESS'}  # 无法归属，确认止血靠日志排查

    if refund_status in ('SUCCESS', 'CHANGE'):
        refund.status = Refund.STATUS_ARRIVED
    elif refund_status in ('FAIL', 'REFUNDCLOSE'):
        logger.error('微信退款未到账 refund=%s status=%s', out_refund_no, refund_status)
        refund.status = Refund.STATUS_FAIL
        _reconcile_order_on_refund(refund, arrived=False)
        logger.info('退款通知已处理 refund=%s status=%s', out_refund_no, refund_status)
        return {'return_code': 'SUCCESS'}
    else:
        logger.warning('退款通知未知状态 refund=%s status=%s', out_refund_no, refund_status)
        return {'return_code': 'SUCCESS'}

    refund.save(update_fields=['status', 'updated_at'])
    _reconcile_order_on_refund(refund, arrived=True)
    logger.info('退款通知已处理 refund=%s status=%s', out_refund_no, refund_status)
    return {'return_code': 'SUCCESS'}
