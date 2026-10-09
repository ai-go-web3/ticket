"""放单客户端：下单后触发放单 + 出票/纠纷回调收敛。

真实字段见 docs/麻花字段级映射.md。
放单接口返回 rtnData=放单号（字符串）；出票靠订单回调 + 查询轮询收敛。
"""
import json
import logging
import re
import time

from django.db import transaction

from apps.common.response import BizError
from apps.order.models import TicketOrder, MahuaDispatch
from apps.order.statemachine import transition
from apps.upadapter.token import get_token
from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE

logger = logging.getLogger('app')

# 放单超时后查单前的等待秒数：麻花处理较慢，超时请求可能仍在处理中，
# 立即查询大概率「查无此单」造成误判重放；等 20s 再查给足受理时间。
DISPATCH_QUERY_DELAY_SECONDS = 20

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

    座位定位只用 row+col，来源链路：麻花座位接口 seatNo（"4排2座"）-> 我方座位
    接口 name -> 前端选座带回 -> 建单 seats_json 快照 -> 此处按文档示例正则
    [a-zA-Z0-9]+ 提取（如 "4排2座" -> row='4', col='2'，"A排5行" -> ('A','5')）。
    不依赖前端传 row/col；快照里的 col 是 0 基画图坐标（columnNo），与放单的
    "座"号不是一个口径，座位名缺失时宁可快速失败也绝不用坐标猜（会买错座）。
    不传 seatId：麻花文档注意3 明确 seatId 易随渠道数据变动而失效，且同时传
    seatId 和 row/col 时麻花优先用 seatId 校验，过期 seatId 会被拒绝
    （实测 rtnCode=100008）。row/col 是文档推荐口径。

    总限价 costTotalPrice：MAHUA_COST_TOTAL_PRICE 环境变量优先（联调防损/成本护栏，
    实际成本高于该价时麻花拒绝出单）；未设置时若已有结算价快照则用作上限。
    放单时结算价通常尚未回填，等于默认不限价——生产建议配置环境变量上限。
    """
    from django.conf import settings as dj_settings

    seats = json.loads(order.seats_json or '[]')
    buy_seats = []
    for i, s in enumerate(seats):
        row, col = _extract_row_col(s.get('name') or '')
        if not row or not col:
            raise BizError(
                f'座位快照缺失座位名，无法定位第{i + 1}个座位（order={order.order_ext_no}），'
                '已阻止放单以防买错座')
        buy_seats.append({'row': row, 'col': col})

    payload = {
        'outId': order.order_ext_no,
        'showId': order.up_schedule_id,
        'buySeats': buy_seats,
        'acceptChangeseat': '1',
    }
    # 出票模式（0特惠/1快速/2极速，见放单文档 §4.1）：特惠不传（麻花默认 0），
    # 快速购票单传 model=1 快速通道，与用户所购模式一致
    if getattr(order, 'buy_mode', '') == 'kuai':
        payload['model'] = 1
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
        - 提交超时/无法解析报文时，等 20s 再查询确认受理状态；查无此单当场重放
          一次，重放仍失败 -> 出票失败并自动退款（见 _converge_pending_dispatch）
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
    except Exception as exc:  # noqa: BLE001 提交超时/非JSON报文：立即收敛，不留多轮补偿
        logger.error('放单请求异常 order=%s err=%s', order_ext_no, exc)
        MahuaDispatch.objects.update_or_create(
            order_ext_no=order.order_ext_no,
            defaults={
                'request_body': payload,
                'response_body': {'error': str(exc)[:500]},
                'dispatch_status': MahuaDispatch.STATUS_PENDING,
            },
        )
        # 失败不当场放弃也不进多轮补偿：查询确认受理状态 -> 查无此单当场重放一次；
        # 重放仍失败 -> 出票失败并自动退款
        return _converge_pending_dispatch(order_ext_no)

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
        _dispatch_fail_converge(order)
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


def _dispatch_fail_converge(order):
    """放单彻底失败收口：订单出票中(20) -> 出票失败(60) + 自动退款。

    收敛中的异常不外抛（微信已扣款，不能回滚支付落库），留日志人工兜底。
    """
    try:
        transition(order, TicketOrder.STATUS_DISPATCH_FAIL)
        from apps.refund.services import auto_refund_dispatch_fail
        auto_refund_dispatch_fail(order)
    except Exception as exc:  # noqa: BLE001 收敛失败留待人工/定时器，不影响支付落库
        logger.error('放单失败后的订单收敛异常 order=%s err=%s', order.order_ext_no, exc)


def _converge_pending_dispatch(order_ext_no):
    """放单提交超时/异常后的即时收敛：等 20s 查询一次 -> 查无此单重放一次。

    文档注意2：超时/异常不代表未受理。先等 DISPATCH_QUERY_DELAY_SECONDS（给
    麻花受理时间），再用我方单号查 /put/query——查到则按查询结果收敛，等麻花
    回调反馈后续状态；查无此单（或查询也异常）才重放，且全生命周期至多重放
    一次（retry_count 兜底，注意1：同单号重复提交可能导致误关单）。
    重放仍失败 -> 出票失败退款。
    """
    time.sleep(DISPATCH_QUERY_DELAY_SECONDS)
    client = MahuaClient()
    try:
        qcode, _qdata = client.query_order(get_token(), order_ext_no)
    except Exception as exc:  # noqa: BLE001 查询异常：无法确认受理状态
        logger.warning('放单失败后查询异常 order=%s err=%s', order_ext_no, exc)
        qcode = None

    if qcode == SUCCESS_CODE:
        # 首次请求实际已被受理：按查询结果收敛，不重放不退款
        logger.info('放单请求失败但麻花已受理，按查询收敛 order=%s', order_ext_no)
        MahuaDispatch.objects.filter(order_ext_no=order_ext_no).update(
            dispatch_status=MahuaDispatch.STATUS_DISPATCHED)
        try:
            query_and_sync(order_ext_no)
        except Exception as exc:  # noqa: BLE001 出票状态留定时器继续同步
            logger.warning('已受理单查询收敛异常 order=%s err=%s', order_ext_no, exc)
        return True, ''

    order = TicketOrder.objects.filter(order_ext_no=order_ext_no, deleted=0).first()
    if not order or order.status != TicketOrder.STATUS_DISPATCHING:
        return False, ''
    d = MahuaDispatch.objects.filter(order_ext_no=order_ext_no).first()
    if d and (d.retry_count or 0) >= 1:
        # 已重放过一次仍不成功：不再重试，转出票失败并退款
        logger.error('放单重放后仍失败，转出票失败并退款 order=%s qcode=%s', order_ext_no, qcode)
        d.dispatch_status = MahuaDispatch.STATUS_FAIL
        d.save(update_fields=['dispatch_status', 'updated_at'])
        _dispatch_fail_converge(order)
        return False, ''
    if d:
        d.retry_count = (d.retry_count or 0) + 1
        d.save(update_fields=['retry_count', 'updated_at'])
    logger.info('放单失败，立即重放一次 order=%s qcode=%s', order_ext_no, qcode)
    return dispatch(order_ext_no)


def query_and_sync(order_ext_no):
    """查询放单状态并同步（字段映射 §4.2）。事件收敛与回调共用 _handle_dispatch_event。"""
    client = MahuaClient()
    token = get_token()
    code, data = client.query_order(token, order_ext_no)

    order = TicketOrder.objects.get(order_ext_no=order_ext_no)
    if code != SUCCESS_CODE or not isinstance(data, dict):
        return order
    _handle_dispatch_event(order, data.get('status'), data)
    return order


def _handle_dispatch_event(order, status, payload):
    """麻花放单事件收敛——查询（/put/query）与回调共用的唯一入口，幂等。

    查询先处理、回调后到（或反之）时不得重复动作：
    - 出票/确认收货/退票：仅当订单仍处于前置状态才迁移（transition 带乐观锁），
      状态已推进则整体跳过；出票回填本身先清后建，updateTicket 多次到达亦幂等。
    - drawClose（出票失败退款）：在 select_for_update 行锁事务内二次确认状态，
      只有仍处于「出票中」才转出票失败并建退款单；先到者处理完后，后到事件
      看到状态已变更直接跳过——禁止退款后再退一次。
    """
    if status in (CALLBACK_DRAW_SUCCESS, CALLBACK_UPDATE):
        # 出票成功 / 更新票（可能多次）；confirmPrice=真实出票结算价（元）→ 分。
        # 先落结算价再走出票迁移，避免 _on_ticketed 的 update_fields 漏存该字段。
        confirm_price = payload.get('confirmPrice')
        if confirm_price is not None and order.settle_amount is None:
            order.settle_amount = int(round(float(confirm_price) * 100))
            order.save(update_fields=['settle_amount', 'updated_at'])
        if order.status == TicketOrder.STATUS_DISPATCHING:
            _on_ticketed(order, payload)
    elif status == CALLBACK_DRAW_CLOSE:
        if order.status != TicketOrder.STATUS_DISPATCHING:
            return
        with transaction.atomic():
            locked = TicketOrder.objects.select_for_update().get(id=order.id)
            if locked.status != TicketOrder.STATUS_DISPATCHING:
                return  # 另一路径（查询/回调）已处理，禁止二次退款
            transition(locked, TicketOrder.STATUS_DISPATCH_FAIL)
            from apps.refund.services import auto_refund_dispatch_fail
            auto_refund_dispatch_fail(locked)
    elif status == CALLBACK_CONFIRM:
        # 确认收货 -> 已完成（触发积分/激励结算）
        if order.status == TicketOrder.STATUS_DISPATCHING:
            transition(order, TicketOrder.STATUS_WAIT_PICK)
        if order.status != TicketOrder.STATUS_DONE:
            transition(order, TicketOrder.STATUS_DONE)
            from apps.distributor.services import confirm_settle
            confirm_settle(order)
    elif status == CALLBACK_TICKET_REFUND:
        # 已退票（票款已退回我方麻花账户）：需对用户原路退款 -> 已退款(80)
        if order.status in (TicketOrder.STATUS_REFUNDING, TicketOrder.STATUS_DISPUTE):
            from apps.refund.services import refund_by_up_ticket_refund
            refund_by_up_ticket_refund(order)


def query_dispatching_orders(limit=100):
    """每 2 分钟轮询「出票中」订单：逐一调 /put/query（我方单号）收敛状态。

    查询结果与回调共用 _handle_dispatch_event 收敛（幂等，先处理者生效，
    后到的回调不会重复出票/退款）。查无此单（rtnCode 非成功）说明放单尚未
    受理，交由放单失败收敛链路（dispatch/补偿任务）处理，这里跳过。
    """
    from django.conf import settings as dj_settings
    if not dj_settings.MAHUA.get('BASE_URL'):
        return {'queried': 0, 'synced': 0}

    res = {'queried': 0, 'synced': 0}
    orders = TicketOrder.objects.filter(
        status=TicketOrder.STATUS_DISPATCHING, deleted=0,
    ).order_by('updated_at')[:limit]
    client = None
    token = None
    for order in orders:
        try:
            client = client or MahuaClient()
            token = token or get_token()
            code, data = client.query_order(token, order.order_ext_no)
        except Exception as exc:  # noqa: BLE001 单笔异常不影响整批
            logger.warning('出票中订单查询异常 order=%s err=%s', order.order_ext_no, exc)
            continue
        if code != SUCCESS_CODE or not isinstance(data, dict):
            continue
        try:
            _handle_dispatch_event(order, data.get('status'), data)
            res['synced'] += 1
        except Exception as exc:  # noqa: BLE001 单笔失败不影响整批
            logger.warning('出票中订单查询收敛异常 order=%s err=%s', order.order_ext_no, exc)
        finally:
            res['queried'] += 1
    return res


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
    """放单补偿任务（内置定时器调用）：收敛 STATUS_PENDING 残留单。

    STATUS_PENDING（放单提交超时/异常的残留，正常已在 dispatch 内即时收敛）：
    先查 /put/query——查得到则按查询结果收敛订单；查不到（rtnCode 非成功，
    麻花未受理该单）说明提交根本没到达，重放一次（retry_count 兜底全生命周期
    至多重放一次）；重放过仍查无此单 -> 出票失败并自动退款，不反复重试。

    「出票中」订单的状态轮询由独立任务 query_dispatching_orders（每 2 分钟
    全量查询）负责，此处不再重复。

    仅当订单仍处于「出票中」时才重放/迁移；已进入退款等后续链路的单不碰。
    """
    from django.conf import settings as dj_settings

    res = {'pending': 0, 'redispatched': 0, 'synced': 0, 'failed': 0}
    if not dj_settings.MAHUA.get('BASE_URL'):
        return {'pending': 0, 'redispatched': 0, 'synced': 0, 'failed': 0}

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
            # 麻花查无此单：提交未受理。至多重放一次（retry_count 全局兜底），
            # 重放过仍查无此单 -> 不再重试，转出票失败并退款
            order = TicketOrder.objects.filter(
                order_ext_no=d.order_ext_no, deleted=0,
            ).first()
            if not order or order.status != TicketOrder.STATUS_DISPATCHING:
                continue
            if (d.retry_count or 0) >= 1:
                logger.error('放单补偿：重放后仍查无此单，转出票失败并退款 order=%s', d.order_ext_no)
                d.dispatch_status = MahuaDispatch.STATUS_FAIL
                d.save(update_fields=['dispatch_status', 'updated_at'])
                _dispatch_fail_converge(order)
                res['failed'] += 1
            else:
                d.retry_count = (d.retry_count or 0) + 1
                d.save(update_fields=['retry_count', 'updated_at'])
                logger.info('放单补偿：查无此单，重放一次 order=%s', d.order_ext_no)
                dispatch(d.order_ext_no)
                res['redispatched'] += 1
        else:
            query_and_sync(d.order_ext_no)
            res['synced'] += 1
    return res


def on_order_callback(payload):
    """麻花订单回调（字段映射 §6.1）。事件收敛走与查询共用的 _handle_dispatch_event
    （幂等：查询先处理的单，回调到达不会重复出票/退款）。"""
    out_id = payload.get('outId')
    status = payload.get('status')
    try:
        order = TicketOrder.objects.get(order_ext_no=out_id)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在')

    _handle_dispatch_event(order, status, payload)

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
