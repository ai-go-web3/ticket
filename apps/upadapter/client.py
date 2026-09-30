"""放单客户端：下单后触发放单 + 出票/纠纷回调收敛。

真实字段见 docs/麻花字段级映射.md。
放单接口返回 rtnData=放单号（字符串）；出票靠订单回调 + 查询轮询收敛。
"""
import json
import logging
import re

from apps.common.response import BizError
from apps.order.models import TicketOrder, MahuaDispatch
from apps.order.statemachine import transition
from apps.upadapter.token import get_token
from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE

logger = logging.getLogger('app')

# 麻花订单回调状态 → 我方动作（见字段映射 §6.1）
CALLBACK_DRAW_SUCCESS = 'drawSuccess'    # 已出票
CALLBACK_UPDATE = 'updateTicket'         # 更新票（可多次）
CALLBACK_DRAW_CLOSE = 'drawClose'        # 出票失败退款
CALLBACK_CONFIRM = 'confirmSuccess'      # 确认收货
CALLBACK_TICKET_REFUND = 'ticketRefund'  # 已退票
CALLBACK_CANCEL_DISPUTE = 'cancelDispute'  # 取消纠纷

# 查询接口 status 枚举（见字段映射 §4.2）
QUERY_DRAW_SUCCESS = 'drawSuccess'
QUERY_CONFIRM = 'confirmSuccess'
QUERY_DRAW_CLOSE = 'drawClose'
QUERY_DISPUTE = 'dispute'
QUERY_TICKET_REFUND = 'ticketRefund'


def _extract_row_col(seat_name):
    """从座位名提取 (row, col)，如 "4排2座" -> ('4', '2')。
    兼容 "A排5行" 等特殊格式：正则取所有字母数字段，第1个=排，第2个=座。
    """
    parts = re.findall(r'[a-zA-Z0-9]+', seat_name or '')
    if len(parts) >= 2:
        return parts[0], parts[1]
    return seat_name, ''


def build_dispatch_payload(order, call_back_url=None):
    """构造放单请求 body（字段映射 §4.1）。

    优先用 row+col（seatId 可能变动）。
    """
    seats = json.loads(order.seats_json or '[]')
    buy_seats = []
    for s in seats:
        row, col = _extract_row_col(s.get('name', ''))
        item = {'row': row, 'col': col}
        # 麻花座位接口的 seatId（前端传 seatId，历史数据用 seat_id）；有则一并带上，
        # 放单时麻花会优先用 seatId 校验，row/col 作为兜底。
        seat_id = s.get('seatId') or s.get('seat_id')
        if seat_id:
            item['seatId'] = seat_id
        buy_seats.append(item)

    payload = {
        'outId': order.order_ext_no,
        'showId': order.up_schedule_id,
        'buySeats': buy_seats,
        'acceptChangeseat': '1',
    }
    if call_back_url:
        payload['callBackUrl'] = call_back_url
    if order.mobile:
        payload['phoneNo'] = order.mobile
    if order.settle_amount:
        payload['costTotalPrice'] = order.settle_amount / 100.0  # 分 -> 元
    return payload


def dispatch(order_ext_no, call_back_url=None):
    """放单：从麻花余额扣款，代出票（异步）。真实扣款，受资金安全开关控制。

    返回 (成功标志, 放单号)。放单只拿放单号，出票结果靠回调 + 查询轮询收敛。

    安全开关 settings.MAHUA['DISPATCH_ENABLED']=False（默认）时：
        只构造并落库真实放单报文（dry-run），绝不调用 /put/add，不扣款；
        返回 (False, 'DRY-RUN')，订单停在「出票中」等待真正放单。
    =True 时：真正调用麻花放单接口。
    """
    from django.conf import settings as dj_settings

    order = TicketOrder.objects.get(order_ext_no=order_ext_no)
    client = MahuaClient()

    # 回调地址：调用方未传则用配置的公网回调地址
    cb_url = call_back_url or dj_settings.MAHUA.get('CALLBACK_URL') or None
    payload = build_dispatch_payload(order, cb_url)

    if not dj_settings.MAHUA.get('DISPATCH_ENABLED'):
        MahuaDispatch.objects.update_or_create(
            order_ext_no=order.order_ext_no,
            defaults={
                'request_body': payload,
                'response_body': {'dryRun': True, 'rtnCode': None,
                                  'note': '放单资金安全开关关闭，未真实调用'},
                'dispatch_status': MahuaDispatch.STATUS_DISPATCHED,
            },
        )
        logger.warning(
            '[DRY-RUN] 放单未执行（DISPATCH_ENABLED=false）order=%s 报文=%s',
            order_ext_no, json.dumps(payload, ensure_ascii=False))
        return False, 'DRY-RUN'

    token = get_token()
    code, data = client.dispatch(token, payload)
    mahua_order_no = data if isinstance(data, str) else (data or {}).get('id', '')

    MahuaDispatch.objects.update_or_create(
        order_ext_no=order.order_ext_no,
        defaults={
            'mahua_order_no': mahua_order_no,
            'request_body': payload,
            'response_body': {'rtnCode': code, 'rtnData': data},
            'dispatch_status': MahuaDispatch.STATUS_DISPATCHED,
        },
    )

    if code != SUCCESS_CODE:
        logger.error('放单失败 order=%s code=%s data=%s', order_ext_no, code, data)
        return False, mahua_order_no

    logger.info('放单成功 order=%s mahua_order_no=%s', order_ext_no, mahua_order_no)
    return True, mahua_order_no


def query_and_sync(order_ext_no):
    """查询放单状态并同步（出票轮询兜底，字段映射 §4.2）。"""
    client = MahuaClient()
    token = get_token()
    code, data = client.query_order(token, order_ext_no)

    order = TicketOrder.objects.get(order_ext_no=order_ext_no)
    if code != SUCCESS_CODE or not isinstance(data, dict):
        return order

    status = data.get('status')
    confirm_price = data.get('confirmPrice')
    if confirm_price is not None:
        order.settle_amount = int(round(float(confirm_price) * 100))
        order.save(update_fields=['settle_amount', 'updated_at'])

    if status == QUERY_DRAW_SUCCESS:
        _on_ticketed(order, data)
    elif status == QUERY_CONFIRM:
        if order.status != TicketOrder.STATUS_DONE:
            transition(order, TicketOrder.STATUS_DONE)
    elif status == QUERY_DRAW_CLOSE:
        if order.status in (TicketOrder.STATUS_DISPATCHING,):
            transition(order, TicketOrder.STATUS_DISPATCH_FAIL)
            from apps.refund.services import auto_refund_dispatch_fail
            auto_refund_dispatch_fail(order)
    return order


def _on_ticketed(order, data):
    """出票成功：回填票券 + 状态迁移。"""
    from apps.order.models import Ticket

    if order.status == TicketOrder.STATUS_DISPATCHING:
        transition(order, TicketOrder.STATUS_WAIT_PICK)

    tickets = data.get('tickets') or []
    real_seats = data.get('realSeats')
    real_seat_list = [s.strip() for s in (real_seats or '').split(',') if s.strip()]

    # 幂等：先清旧票再建（updateTicket 场景会多次回调）
    Ticket.objects.filter(order_id=order.id).delete()
    objs = []
    for i, t in enumerate(tickets):
        seat = real_seat_list[i] if i < len(real_seat_list) else ''
        objs.append(Ticket(
            order_id=order.id,
            seat_no=seat or f'票{i + 1}',
            ticket_code=t.get('ticketInfo'),
            barcode=t.get('ticketImg'),
            status=Ticket.STATUS_WAIT_PICK,
        ))
    if objs:
        Ticket.objects.bulk_create(objs)


def on_order_callback(payload):
    """麻花订单回调（字段映射 §6.1）。收敛出票结果（幂等由 view 层保证）。"""
    out_id = payload.get('outId')
    status = payload.get('status')
    try:
        order = TicketOrder.objects.get(order_ext_no=out_id)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在')

    if status == CALLBACK_DRAW_SUCCESS or status == CALLBACK_UPDATE:
        # 出票成功 / 更新票（可能多次回调）
        if order.status == TicketOrder.STATUS_DISPATCHING:
            _on_ticketed(order, payload)
    elif status == CALLBACK_DRAW_CLOSE:
        # 出票失败 -> 自动退款
        if order.status == TicketOrder.STATUS_DISPATCHING:
            transition(order, TicketOrder.STATUS_DISPATCH_FAIL)
            from apps.refund.services import auto_refund_dispatch_fail
            auto_refund_dispatch_fail(order)
    elif status == CALLBACK_CONFIRM:
        # 确认收货 -> 已完成（触发佣金结算）
        if order.status != TicketOrder.STATUS_DONE:
            transition(order, TicketOrder.STATUS_DONE)
            from apps.distributor.services import settle_commission
            settle_commission(order)
    elif status == CALLBACK_TICKET_REFUND:
        # 已退票（关注 ticketRefundFee）
        if order.status not in (TicketOrder.STATUS_REFUNDED,):
            transition(order, TicketOrder.STATUS_REFUNDED)

    # 更新放单映射（成交价 + 关闭原因 + 退票手续费）
    update_fields = {}
    if payload.get('confirmPrice') is not None:
        update_fields['mahua_order_no'] = payload.get('outId')
    if payload.get('note'):
        order.close_reason = payload.get('note')
        order.save(update_fields=['close_reason', 'updated_at'])
    MahuaDispatch.objects.filter(order_ext_no=out_id).update(
        response_body=payload,
        dispatch_status=MahuaDispatch.STATUS_TICKETED if status in (CALLBACK_DRAW_SUCCESS, CALLBACK_UPDATE, CALLBACK_CONFIRM) else MahuaDispatch.STATUS_FAIL,
    )
    return order
