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
        goods_due = ticket_amount + service_fee - discount   # 用券前应付（分）
        if goods_due < 0:
            goods_due = 0

        # 联调开关：环境变量强制实付金额（如 1 分钱走真实微信支付→放单全链路）
        from django.conf import settings as dj_settings
        override_fen = int(getattr(dj_settings, 'PAY_AMOUNT_OVERRIDE_FEN', 0) or 0)

        # 代金券抵扣：积分抵扣购票已下线，票款一律现金支付，但可用观影代金券减现金。
        # 先定订单号（锁券需回写占用归属），再在锁券同事务内校验+置 LOCKED，得抵扣额封顶到应付。
        ext_no = gen_order_ext_no()
        voucher_no = (payload.get('voucherNo') or '').strip()
        voucher_amount = 0
        if voucher_no:
            from apps.points import services as voucher_services
            voucher_amount = voucher_services.lock_voucher(
                user_id, voucher_no, ext_no, goods_due)

        pay_amount = goods_due - voucher_amount
        if pay_amount < 0:
            pay_amount = 0
        if override_fen > 0:
            pay_amount = override_fen

        # 3. 落单（幂等由微信支付回调 pay_no 幂等 + 放单 outId 幂等兜底）
        order = TicketOrder.objects.create(
            order_ext_no=ext_no,
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
            voucher_no=voucher_no if voucher_amount > 0 else '',
            voucher_amount=voucher_amount if voucher_amount > 0 else 0,
            mobile=mobile,
            status=TicketOrder.STATUS_PAYING,
            price_rate=rate,
            est_cost_amount=est_cost or None,
            buy_mode=buy_mode,
        )

        # 订阅消息：入队「催付」定时提醒（到点=付款倒计时剩 ~N 分钟，发送前复检是否仍未支付）。
        # 走 on_commit：建单事务提交后才落任务，避免回滚单也发通知；通知异常已在服务内吞掉。
        from apps.notify import services as notify_services
        transaction.on_commit(lambda: notify_services.enqueue_pay_remind(order))
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

        # 代金券核销：支付成功，占用中(LOCKED)券转已使用(USED)。同事务、幂等；
        # 绝不因券异常回滚已扣款的支付落库——异常仅记录，放单/退款链路兜底。
        if order.voucher_no:
            try:
                from apps.points import services as voucher_services
                voucher_services.consume_voucher(order.voucher_no, order.order_ext_no)
            except Exception as exc:  # noqa: BLE001
                logger.error('券核销异常 order=%s voucher=%s err=%s',
                             order.order_ext_no, order.voucher_no, exc)

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

        # 订阅消息：入队「开场前取票」提醒（到点=开场前 ~N 分钟）。此刻票尚未出，
        # 取票码发送时现查；发送前复检订单仍为「待取票」且未过开场，否则自动跳过。
        # 放单成败都不影响入队——复检兜底。走 on_commit 保证支付事务提交后才落任务。
        from apps.notify import services as notify_services
        transaction.on_commit(lambda: notify_services.enqueue_pickup_remind(order))
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
    restore_order_voucher(order)
    logger.info('超时未付订单已自动关闭 order=%s', order.order_ext_no)
    return True


def restore_order_voucher(order):
    """订单终结（未支付关单 / 用户取消 / 出票失败退款）回滚代金券。

    券处于占用中(LOCKED)→退回未使用；已核销(USED)→退款回滚未使用；均已过期则转 EXPIRED。
    由 apps.points.services.restore_voucher 幂等完成。异常吞掉仅记录——
    券回滚失败绝不得阻断关单/退款主流程。
    """
    if not order.voucher_no:
        return
    try:
        from apps.points import services as voucher_services
        voucher_services.restore_voucher(order.voucher_no, order.order_ext_no)
    except Exception as exc:  # noqa: BLE001
        logger.error('订单终结回滚券异常 order=%s voucher=%s err=%s',
                     order.order_ext_no, order.voucher_no, exc)


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


def screen_after_show_minutes():
    """开场后到「已放映」的放映缓冲（分钟）：now >= 开场时间 + N 分钟才收敛。

    取默认 150 分钟（约一场影片放映时长），避免刚开场仍在放映中就被判「已放映」，
    也与「开场前催取票 / 距开场≥2h 可纠纷退票」窗口互不干扰。可经环境变量调节。
    """
    from django.conf import settings as dj_settings
    return int(getattr(dj_settings, 'SCREEN_AFTER_SHOW_MINUTES', 150) or 0)


def screen_order(order):
    """把「待取票」单收敛为终态「已放映」，并在此入账消费返积分。

    「已放映」即观影进度终点（不再流转到已完成）。置为已放映后调用 confirm_settle
    发放消费返积分——earn_consume 以 order_ext_no 为 ref_no 幂等去重（同单只返一次），
    且 confirm_settle 内部吞异常不外抛，故不会因积分侧问题影响状态收敛与整批扫描。

    保证「只有待取票能转已放映」，三重锁：
    1) 调用方 mark_screened_orders 只捞 status=待取票 的单；
    2) transition 校验迁移表，唯 STATUS_WAIT_PICK 开 SCREENED 出边，其它态直达报错；
    3) require_status 在 UPDATE 的 WHERE 再锁库内仍为待取票——即便并发/脏读使该行
       已被推进（且未来若出现不升 version 的状态写入也不会被覆盖），非待取票一律跳过。
    迁移失败（状态已变）静默返回 False，绝不阻断整批扫描。
    """
    from apps.order.statemachine import transition
    try:
        transition(order, TicketOrder.STATUS_SCREENED,
                   require_status=TicketOrder.STATUS_WAIT_PICK)
    except BizError as exc:  # noqa: BLE001 状态已变更：跳过该单
        logger.info('已放映收敛跳过（状态已变更） order=%s err=%s', order.order_ext_no, exc)
        return False
    # 已放映终态：发放消费返积分（幂等，confirm_settle 内部已 try/except，不会抛出）
    from apps.distributor.services import confirm_settle
    confirm_settle(order)
    logger.info('待取票->已放映（终态，已入积分）：开场缓冲已到 order=%s', order.order_ext_no)
    return True


def mark_screened_orders(limit=500):
    """定时任务入口：扫描「待取票」且已过「开场 + 放映缓冲」的订单，置为终态「已放映」。

    迁移成功后在 screen_order 内发放消费返积分（已放映即观影终点，不再等麻花确认收货）。
    分批 + 逐单乐观锁/写时状态锁，天然幂等：已迁到「已放映」的单被查询条件排除；
    积分以 order_ext_no 去重，重复执行不会重复入账。返回本次成功收敛数量。
    """
    deadline = timezone.now() - timedelta(minutes=screen_after_show_minutes())

    screened = 0
    # 1) 主路径：建单已写开场快照 show_start_at 的订单（新单必走此路，可走索引区间）
    for order in TicketOrder.objects.filter(
        status=TicketOrder.STATUS_WAIT_PICK, deleted=0,
        show_start_at__isnull=False, show_start_at__lte=deadline,
    ).order_by('show_start_at')[:limit]:
        if screen_order(order):
            screened += 1

    # 2) 兜底：迁移 0005 之前 show_start_at 为空的历史单，按排片表回退逐单判断是否到点
    #    （schedule_id 非外键、无法 join，故取有限候选在内存判定；存量收敛后此路趋零）
    if screened < limit:
        for order in TicketOrder.objects.filter(
            status=TicketOrder.STATUS_WAIT_PICK, deleted=0,
            show_start_at__isnull=True,
        ).order_by('id')[:limit]:
            start_at = order_show_start_at(order)
            if start_at is None or start_at > deadline:
                continue
            if screen_order(order):
                screened += 1
            if screened >= limit:
                break

    return screened
