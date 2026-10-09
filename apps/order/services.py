"""订单服务：建单 / 触发放单 / 查询。"""
import json
import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_order_ext_no
from apps.order.models import TicketOrder, OrderPayment, MahuaDispatch
from apps.catalog import services as catalog_services
from apps.catalog import markup
from apps.catalog.models import Schedule, Movie, Cinema

logger = logging.getLogger('app')


def order_show_start_at(order):
    """订单开场时间：优先建单快照 show_start_at，缺失回退排片表；均无返回 None。"""
    if getattr(order, 'show_start_at', None):
        return order.show_start_at
    return Schedule.objects.filter(
        id=order.schedule_id).values_list('start_at', flat=True).first()


def create_order(user_id, payload):
    """建「待支付」单。

    payload: { scheduleId, seats, mobile, discountAmount }
    不做本地锁座：座位最终是否可得由麻花放单结果收敛（放单失败走自动退款）。
    """
    schedule_id = payload['scheduleId']
    seats = payload['seats']
    mobile = payload.get('mobile') or ''
    discount = payload.get('discountAmount', 0)

    # 取票手机号：接口不再回传明文手机号，客户端通常不带 mobile，
    # 这里回落到登录用户绑定的手机号（服务端库内），保证麻花出票拿得到取票号。
    if not mobile:
        from apps.auths.models import AppUser
        mobile = AppUser.objects.filter(id=user_id).values_list('phone', flat=True).first() or ''

    with transaction.atomic():
        # 1. 校验场次可售（未删 + 在售 + 未过停售线，与列表/选座同一口径）
        try:
            schedule = Schedule.objects.get(id=schedule_id, deleted=0)
        except Schedule.DoesNotExist:
            raise BizError('场次不存在', code=40400)
        catalog_services.ensure_sellable(schedule)

        # 座位快照校验：放单 row/col 由座位名（name，源自麻花 seatNo）正则提取，
        # 名字缺失只能到放单时才暴露；建单即拦截并给出明确报错。
        if any(not str(s.get('name') or '').strip() for s in seats):
            raise BizError('座位信息缺失，请重新选座', code=40000)

        # 2. 计算金额（分）：不收服务费，总费 = ΣsalePrice（成本上浮后的每座售价求和）。
        #    快速模式按 maxSpeedPrice 收费、放单走快速通道（model=1），预估成本按原始 maxSpeedPrice 口径；
        #    特惠模式按 fastPrice 收费、放单默认特惠通道，预估成本按原始 fastPrice 口径。
        #    上浮口径与该场次取价（pull_seats）完全一致：同一条命中规则 (mode, rate, flat_fen)。
        mode, rate, flat_fen = markup.resolve_for_schedule(schedule)
        buy_mode = payload.get('buyMode') or 'tehui'

        def _raw_cost(fen):
            """上浮后售价(分)反推原始成本价(分)：按命中规则口径反向。

            rate 模式 raw=round(fen/(1+rate))（±1分舍入）；flat 模式 raw=fen-flat_fen。
            注意：仅在结算价确实是按此规则上浮时反推才成立；若规则在取价后变更会引入误差。
            """
            return markup.reverse_cost_fen(fen, mode, rate, flat_fen)

        ticket_amount = 0
        est_fast = 0
        est_max = 0
        for s in seats:
            sale = int(s.get('salePrice') or s['price'])
            # 售价不得高于原价：防前端异常/数据缺失造成反向计价
            if int(s.get('price') or 0) > 0:
                sale = min(sale, int(s['price']))
            ticket_amount += sale
            fast = int(s.get('fastPrice') or 0)
            if fast > 0:
                s['rawFast'] = _raw_cost(fast)
                est_fast += s['rawFast']
            ms = int(s.get('maxSpeedPrice') or 0)
            if ms > 0:
                s['rawMaxSpeed'] = _raw_cost(ms)
                est_max += s['rawMaxSpeed']
            s['salePrice'] = sale
        est_cost = est_max if buy_mode == 'kuai' else est_fast
        service_fee = 0
        goods_due = ticket_amount + service_fee - discount
        if goods_due < 0:
            goods_due = 0

        # 联调开关：环境变量强制实付金额（如 1 分钱走真实微信支付→放单全链路）
        from django.conf import settings as dj_settings
        override_fen = int(getattr(dj_settings, 'PAY_AMOUNT_OVERRIDE_FEN', 0) or 0)

        # 积分抵扣（合规：仅用户自身积分抵扣现金票款；服务端重算，绝不信任前端传值）
        #   - 前端传 pointDeduct=期望抵扣的积分数（0=不使用）；
        #   - 服务端按 quote_deduct 用余额/比例/最低自付/上限重算可得积分；
        #   - pay_amount 恒为「微信现金」= goods_due − 积分抵扣金额（分）；
        #   - override 联调态强制实付，此时不使用积分抵扣，避免二者打架。
        from apps.distributor import services as dist_services
        point_deduct = 0
        point_deduct_value = 0
        want_points = int(payload.get('pointDeduct', 0) or 0)
        if (dist_services._points_enabled() and want_points > 0
                and goods_due > 0 and override_fen <= 0):
            q = dist_services.quote_deduct(goods_due, user_id)
            fpp = q['fenPerPoint'] or 1
            allowed = min(want_points, q['points'])
            if allowed > 0:
                value = int(round(allowed * fpp))
                # 双保险：抵扣后现金自付不得低于最低自付
                if goods_due - value < q['minSelfPay']:
                    value = max(goods_due - q['minSelfPay'], 0)
                    allowed = int(value // fpp) if fpp else 0
                    value = int(round(allowed * fpp))
            if allowed > 0:
                point_deduct = allowed
                point_deduct_value = value

        pay_amount = goods_due - point_deduct_value
        if pay_amount < 0:
            pay_amount = 0
        if override_fen > 0:
            point_deduct = 0
            point_deduct_value = 0
            pay_amount = override_fen

        # 3. 落单（幂等由微信支付回调 pay_no 幂等 + 放单 outId 幂等兜底）
        order = TicketOrder.objects.create(
            order_ext_no=gen_order_ext_no(),
            user_id=user_id,
            schedule_id=schedule_id,
            cinema_id=schedule.cinema_id,
            movie_id=schedule.movie_id,
            up_schedule_id=schedule.up_schedule_id,
            show_start_at=schedule.start_at,
            seats_json=json.dumps(seats, ensure_ascii=False),
            seat_count=len(seats),
            ticket_amount=ticket_amount,
            service_fee=service_fee,
            discount_amount=discount,
            pay_amount=pay_amount,
            point_deduct=point_deduct,
            point_deduct_value=point_deduct_value,
            mobile=mobile,
            status=TicketOrder.STATUS_PAYING,
            price_rate=rate,
            est_cost_amount=est_cost or None,
            buy_mode=buy_mode,
        )
        # 同事务内冻结抵扣积分（balance→frozen）：建单与积分占用原子，避免并发双花。
        if point_deduct > 0:
            dist_services.freeze_deduct(user_id, point_deduct, order.order_ext_no)
    return order


def mark_paid(order_id, pay_no, amount, callback_raw=None):
    """支付成功：落流水 + 标记已付 + 触发放单（同一事务）。

    事务边界（同一事务内完成）：
        1. 支付流水落库（pay_no 唯一幂等，防微信重复回调）
        2. 订单状态迁移：待付款(10) -> 出票中(20)
        3. 本地生成放单订单（mahua_dispatch 落库）
        4. 调用麻花 /api/movie-server/movie/put/add 真实放单

    竞态处理：超时关单/用户取消与支付回调并发时，微信侧已扣款而订单已关闭——
    不能吞掉这笔钱：对非「待付款」状态收到的新支付流水一律原路全额退款
    （_refund_after_close），退款发起失败由退款重试任务兜底。

    放单请求超时/返回异常时不能回滚整个事务（微信侧已扣款）：
    dispatch() 内部即时收敛——等 20s 后查询确认受理状态（有单则等麻花回调），
    查无此单当场重放一次；重放仍失败则订单转「出票失败」并自动退款，
    不做多轮补偿重试。
    """
    from apps.order.statemachine import transition

    try:
        order = TicketOrder.objects.get(id=order_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)

    with transaction.atomic():
        # 支付流水（pay_no 唯一幂等）
        _, created = OrderPayment.objects.get_or_create(
            pay_no=pay_no,
            defaults={
                'order_id': order_id,
                'amount': amount,
                'status': OrderPayment.STATUS_SUCCESS,
                'callback_raw': callback_raw,
                'paid_at': timezone.now(),
            },
        )
        if not created:
            return order  # 重复回调

        if order.status != TicketOrder.STATUS_PAYING:
            # 关单/退款态之后才到账的新支付（关单竞态、重复支付）：原路退款
            _refund_after_close(order, amount)
            return order

        order.pay_status = TicketOrder.PAY_DONE
        order.save(update_fields=['pay_status', 'updated_at'])

        # 状态迁移：待付款 -> 出票中
        transition(order, TicketOrder.STATUS_DISPATCHING)

        # 支付成功即核销本单抵扣积分（frozen 正式扣减）。放在事务内：
        # 若积分核销冲突则连同支付落库一起回滚，微信重发回调时幂等收敛。
        if getattr(order, 'point_deduct', 0) > 0:
            from apps.distributor import services as dist_services
            dist_services.settle_deduct(
                order.user_id, order.point_deduct, order.order_ext_no)

        # 同一事务内：本地生成放单订单 + 调用麻花放单接口 /api/movie-server/movie/put/add
        try:
            from apps.upadapter.client import dispatch
            dispatched, mahua_no = dispatch(order.order_ext_no)
        except Exception as exc:  # noqa: BLE001 兜底：不因放单异常回滚支付落库
            logger.error('放单事务内异常 order=%s err=%s', order.order_ext_no, exc)
        else:
            if not dispatched and mahua_no != 'DRY-RUN':
                logger.warning(
                    '放单未完成（已即时收敛：重放一次，仍失败已转出票失败退款） order=%s mahua_no=%s',
                    order.order_ext_no, mahua_no)
    return order


def _refund_after_close(order, amount):
    """订单非「待付款」状态下收到支付到账（竞态/重复支付）：原路全额退款。

    订单已不在待付款态，不再走状态机；只落退款单 + 发起微信退款。
    发起失败时退款单记 FAIL，由退款重试任务（retry_failed_refunds）兜底重发。
    """
    from apps.common.utils import gen_refund_no
    from apps.refund.models import Refund
    from apps.refund.services import refund_retry_once

    logger.warning(
        '支付到账时订单已非待付款，自动原路退款 order=%s status=%s amount=%s分',
        order.order_ext_no, order.status, amount)
    refund = Refund.objects.create(
        refund_ext_no=gen_refund_no(),
        order_id=order.id,
        type=Refund.TYPE_PAY_AFTER_CLOSE,
        reason='订单关闭后支付到账，自动原路退款',
        refund_amount=amount,
        status=Refund.STATUS_REFUNDING,
    )
    try:
        refund_retry_once(order, refund)
    except Exception as exc:  # noqa: BLE001 受理失败：退款单记 FAIL 交重试任务
        refund.status = Refund.STATUS_FAIL
        refund.save(update_fields=['status', 'updated_at'])
        logger.error('关单竞态退款发起失败 order=%s err=%s', order.order_ext_no, exc)


def query_order(order_id, user_id=None):
    """查询订单详情。待付款单已超时则先惰性关闭再返回（定时器兜底前的即时收敛）。"""
    try:
        order = TicketOrder.objects.get(id=order_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)
    if user_id is not None and order.user_id != user_id:
        raise BizError('无权查看该订单', code=ErrorCode.FORBIDDEN)
    close_if_expired(order)
    if order.status == TicketOrder.STATUS_PAYING:
        order.refresh_from_db()
    return order


def pay_timeout_seconds():
    """付款超时时间（秒），settings.PAY_TIMEOUT 默认 15 分钟。"""
    from django.conf import settings as dj_settings
    return int(getattr(dj_settings, 'PAY_TIMEOUT', 900))


def is_pay_expired(order):
    """待付款订单是否已超过付款时限。"""
    from django.utils import timezone
    return (timezone.now() - order.created_at).total_seconds() >= pay_timeout_seconds()


def close_if_expired(order):
    """待付款订单若已超时则关闭（惰性关单入口），返回是否关闭。"""
    if order.status != TicketOrder.STATUS_PAYING or not is_pay_expired(order):
        return False
    return _close_expired(order)


def _close_expired(order):
    """关闭超时未付订单：待付款 -> 已关闭。

    并发兜底：关闭瞬间用户可能刚好支付成功（状态/版本已变），
    迁移失败时静默放弃，交给支付链路处理。
    """
    from apps.order.statemachine import transition
    try:
        transition(order, TicketOrder.STATUS_CLOSED)
    except BizError as exc:
        logger.info('超时关单跳过（状态已变更） order=%s err=%s', order.order_ext_no, exc)
        return False
    TicketOrder.objects.filter(id=order.id).update(close_reason='超时未支付自动关闭')
    # 解冻本单冻结的抵扣积分（frozen->balance，幂等；失败不阻断关单，留痕人工补）
    if getattr(order, 'point_deduct', 0) > 0:
        try:
            from apps.distributor import services as dist_services
            dist_services.unfreeze_deduct(
                order.user_id, order.point_deduct, order.order_ext_no)
        except Exception as exc:  # noqa: BLE001
            logger.error('超时关单解冻积分失败 order=%s err=%s', order.order_ext_no, exc)
    logger.info('超时未付订单已自动关闭 order=%s', order.order_ext_no)
    return True


def close_expired_orders():
    """批量关闭超时未付款订单（定时任务调用），返回关闭数量。"""
    from django.utils import timezone
    deadline = timezone.now() - timedelta(seconds=pay_timeout_seconds())
    expired = TicketOrder.objects.filter(
        status=TicketOrder.STATUS_PAYING, created_at__lt=deadline, deleted=0,
    )[:200]
    closed = 0
    for order in expired:
        if _close_expired(order):
            closed += 1
    return closed
