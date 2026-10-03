"""放单客户端：下单后触发放单 + 出票/纠纷回调收敛。

真实字段见 docs/麻花字段级映射.md。
放单接口返回 rtnData=放单号（字符串）；出票靠订单回调 + 查询轮询收敛。
"""
import json
import logging
import re
from datetime import timedelta

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


def _log_payload(payload):
    """放单入参日志副本：脱敏 phoneNo（日志文件不加密，避免明文手机号落盘）。"""
    p = dict(payload or {})
    pn = str(p.get('phoneNo') or '')
    if pn:
        p['phoneNo'] = pn[:3] + '****' + pn[-4:] if len(pn) >= 7 else '****'
    return p


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
    总限价 costTotalPrice：MAHUA_COST_TOTAL_PRICE 环境变量优先（联调防损/成本护栏，
    实际成本高于该价时麻花拒绝出单）；未设置时若已有结算价快照则用作上限。
    放单时结算价通常尚未回填，等于默认不限价——生产建议配置环境变量上限。
    """
    from django.conf import settings as dj_settings

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
    # 出票模式（0特惠/1快速/2极速，见放单文档 §4.1）：特惠不传（麻花默认 0），
    # 快速购票单传 model=2 极速通道，与用户所购模式一致
    if getattr(order, 'buy_mode', '') == 'kuai':
        payload['model'] = 2
    if call_back_url:
        payload['callBackUrl'] = call_back_url
    if order.mobile:
        payload['phoneNo'] = order.mobile

    cap = (dj_settings.MAHUA.get('COST_TOTAL_PRICE') or '').strip()
    if cap:
        try:
            payload['costTotalPrice'] = float(cap)
        except ValueError:
            logger.warning('MAHUA_COST_TOTAL_PRICE 配置非法，忽略: %r', cap)
    elif order.settle_amount:
        payload['costTotalPrice'] = order.settle_amount / 100.0  # 分 -> 元
    return payload


def dispatch(order_ext_no, call_back_url=None):
    """放单：本地生成放单订单（mahua_dispatch）+ 调用麻花 /api/movie-server/movie/put/add。

    调用方必须把本函数包在 transaction.atomic() 内：本地放单订单落库与真实放单
    调用在同一事务（付款成功链路的要求）。本函数内部吞掉网络异常，绝不向外抛——
    支付已成功（微信侧已扣款），不能因放单异常回滚支付落库。

    返回 (成功标志, 放单号)。放单只拿放单号，出票结果靠回调 + 查询轮询收敛。

    安全开关 settings.MAHUA['DISPATCH_ENABLED']=False（默认）时：
        只构造并落库真实放单报文（dry-run），绝不调用 /put/add，不扣款；
        返回 (False, 'DRY-RUN')，订单停在「出票中」等待真正放单。
    =True 时：真正调用麻花放单接口。

    按 docs/mahua-api/03-放单-22-放单.md 注意事项：
        - 相同单号勿重复提交（outId 幂等由 update_or_create + 调用方保证）
        - 提交超时/无法解析报文时，标记 STATUS_PENDING，由补偿任务走查询接口收敛
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
            '[DRY-RUN] 放单未执行（DISPATCH_ENABLED=false）order=%s 入参=%s',
            order_ext_no, json.dumps(_log_payload(payload), ensure_ascii=False))
        return False, 'DRY-RUN'

    try:
        token = get_token()
        logger.info(
            '[放单请求] order=%s 入参=%s',
            order_ext_no, json.dumps(_log_payload(payload), ensure_ascii=False))
        code, data = client.dispatch(token, payload)
    except Exception as exc:  # noqa: BLE001 提交超时/非JSON报文：留待查询接口补偿
        logger.error('放单请求异常（待补偿） order=%s err=%s', order_ext_no, exc)
        MahuaDispatch.objects.update_or_create(
            order_ext_no=order.order_ext_no,
            defaults={
                'request_body': payload,
                'response_body': {'error': str(exc)[:500]},
                'dispatch_status': MahuaDispatch.STATUS_PENDING,
            },
        )
        return False, ''

    logger.info(
        '[放单响应] order=%s 出参=rtnCode=%s rtnData=%s',
        order_ext_no, code,
        data if isinstance(data, str) else json.dumps(data, ensure_ascii=False))

    mahua_order_no = data if isinstance(data, str) else (data or {}).get('id', '')

    if code != SUCCESS_CODE:
        # 明确被拒（拿到 rtnCode 但非成功，如超限价/余额不足）：麻花未受理该单，
        # 之后不会有任何回调。放单订单记失败，我方订单转「出票失败」并自动退款，
        # 防止永远卡在「出票中」。（超时/异常情形走上面 except 分支，用查询补偿）
        MahuaDispatch.objects.update_or_create(
            order_ext_no=order.order_ext_no,
            defaults={
                'mahua_order_no': mahua_order_no,
                'request_body': payload,
                'response_body': {'rtnCode': code, 'rtnData': data},
                'dispatch_status': MahuaDispatch.STATUS_FAIL,
            },
        )
        logger.error('放单被麻花拒绝 order=%s code=%s data=%s', order_ext_no, code, data)
        try:
            transition(order, TicketOrder.STATUS_DISPATCH_FAIL)
            from apps.refund.services import auto_refund_dispatch_fail
            auto_refund_dispatch_fail(order)
        except Exception as exc:  # noqa: BLE001 收敛失败留待人工/定时器，不影响支付落库
            logger.error('放单拒绝后的订单收敛异常 order=%s err=%s', order_ext_no, exc)
        return False, mahua_order_no

    MahuaDispatch.objects.update_or_create(
        order_ext_no=order.order_ext_no,
        defaults={
            'mahua_order_no': mahua_order_no,
            'request_body': payload,
            'response_body': {'rtnCode': code, 'rtnData': data},
            'dispatch_status': MahuaDispatch.STATUS_DISPATCHED,
        },
    )
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
        if order.status == TicketOrder.STATUS_DISPATCHING:
            transition(order, TicketOrder.STATUS_WAIT_PICK)
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


def compensate_dispatches(limit=50):
    """放单补偿任务（内置定时器调用），闭环收敛两类卡单：

    1. STATUS_PENDING（放单提交超时/异常，文档约定用查询接口收敛）：
       先查 /put/query——查得到则按查询结果收敛订单；查不到（rtnCode 非成功，
       麻花未受理该单）说明提交根本没到达，重新放单（outId 幂等，不会重复扣款）。
    2. 放单成功但回调丢失：订单长时间停在「出票中」（超过 DISPATCH_STALE_SECONDS
       无状态更新），主动查询放单结果收敛。

    仅当订单仍处于「出票中」时才重放/迁移；已进入退款等后续链路的单不碰。
    """
    from django.conf import settings as dj_settings
    from django.utils import timezone

    if not dj_settings.MAHUA.get('BASE_URL'):
        return {'pending': 0, 'redispatched': 0, 'synced': 0, 'stale': 0}

    res = {'pending': 0, 'redispatched': 0, 'synced': 0, 'stale': 0}

    pendings = MahuaDispatch.objects.filter(
        dispatch_status=MahuaDispatch.STATUS_PENDING,
    )[:limit]
    for d in pendings:
        res['pending'] += 1
        try:
            client = MahuaClient()
            code, data = client.query_order(get_token(), d.order_ext_no)
        except Exception as exc:  # noqa: BLE001 网络/非JSON报文：下轮再试
            logger.warning('放单补偿查询异常 order=%s err=%s', d.order_ext_no, exc)
            continue
        if code != SUCCESS_CODE:
            # 麻花查无此单：提交未受理，重新放单（outId 幂等）
            order = TicketOrder.objects.filter(
                order_ext_no=d.order_ext_no, deleted=0,
            ).first()
            if order and order.status == TicketOrder.STATUS_DISPATCHING:
                logger.info('放单补偿：查无此单，重新放单 order=%s', d.order_ext_no)
                dispatch(d.order_ext_no)
                res['redispatched'] += 1
        else:
            query_and_sync(d.order_ext_no)
            res['synced'] += 1

    # 回调丢失兜底：出票中超阈值的订单主动查询
    stale_seconds = int(getattr(dj_settings, 'DISPATCH_STALE_SECONDS', 180) or 180)
    deadline = timezone.now() - timedelta(seconds=stale_seconds)
    stale_orders = TicketOrder.objects.filter(
        status=TicketOrder.STATUS_DISPATCHING, deleted=0, updated_at__lt=deadline,
    )[:limit]
    for order in stale_orders:
        try:
            query_and_sync(order.order_ext_no)
            res['stale'] += 1
        except Exception as exc:  # noqa: BLE001 单笔失败不影响整批
            logger.warning('出票轮询兜底异常 order=%s err=%s', order.order_ext_no, exc)
    return res


def on_order_callback(payload):
    """麻花订单回调（字段映射 §6.1）。收敛出票结果（幂等由 view 层保证）。"""
    out_id = payload.get('outId')
    status = payload.get('status')
    try:
        order = TicketOrder.objects.get(order_ext_no=out_id)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在')

    if status == CALLBACK_DRAW_SUCCESS or status == CALLBACK_UPDATE:
        # 出票成功 / 更新票（可能多次回调）；confirmPrice=真实出票结算价（元）→ 分。
        # 先落结算价再走出票迁移，避免 _on_ticketed 的 update_fields 漏存该字段。
        confirm_price = payload.get('confirmPrice')
        if confirm_price is not None and order.settle_amount is None:
            order.settle_amount = int(round(float(confirm_price) * 100))
            order.save(update_fields=['settle_amount', 'updated_at'])
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
        # 已退票（票款已退回我方麻花账户）：需对用户原路退款 -> 已退款(80)
        if order.status in (TicketOrder.STATUS_REFUNDING, TicketOrder.STATUS_DISPUTE):
            from apps.refund.services import refund_by_up_ticket_refund
            refund_by_up_ticket_refund(order)

    # 更新放单映射（关闭原因 + 按回调状态收敛放单状态）
    if payload.get('note'):
        order.close_reason = payload.get('note')
        order.save(update_fields=['close_reason', 'updated_at'])
    if status in (CALLBACK_DRAW_SUCCESS, CALLBACK_UPDATE, CALLBACK_CONFIRM):
        new_ds = MahuaDispatch.STATUS_TICKETED
    elif status == CALLBACK_TICKET_REFUND:
        new_ds = MahuaDispatch.STATUS_REFUNDED
    else:
        new_ds = MahuaDispatch.STATUS_FAIL
    MahuaDispatch.objects.filter(order_ext_no=out_id).update(
        response_body=payload, dispatch_status=new_ds,
    )
    return order
