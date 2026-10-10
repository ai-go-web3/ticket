"""退款服务：拦截取消 / 纠纷退票 / 出票失败自动退 / 纠纷回调。

完整状态链路（配合 apps/order/statemachine.py）：
- 待付款(10)  --取消订单--> 已关闭(50)            （apps/order/views.cancel_order）
- 待付款(10)  --支付成功--> 出票中(20)            （apps/order/services.mark_paid）
- 出票中(20)  --出票成功--> 待取票(30) --(开场+缓冲,定时任务)--> 已放映(35, 终态;消费返积分在此入账)
                 （已完成(40) 保留为历史/兼容终态；麻花 confirmSuccess 仅回补结算价不改状态，新单不再进入）
- 出票中(20)  --拦截成功--> 退款中(70) --到账-->   已退款(80)
- 出票中(20)  --出票失败--> 出票失败(60) --自动退--> 退款中(70) -> 已退款(80)
- 待取票(30)  --发起纠纷(/movie/put/dispute)--> 纠纷中(90)   （dispute_config/apply_dispute，
                                                               距开场>=2小时才可发起）
- 纠纷中(90)  --麻花同意退票(1005/1006)--> 退款中(70) -> 已退款(80)
- 纠纷中(90)  --取消纠纷(1001/1003/1007)/判责(1002/1004/1008)--> 待取票(30)
- 纠纷中(90)  --麻花已退票回调(ticketRefund)--> 已退款(80)

退款顺序：先拿麻花拦截/纠纷结果，再对用户发起微信退款（v2 /secapi/pay/refund，
已对接，见 apps/pay/services.wx_refund）。微信退款被拒时退款单记 FAIL，
由退款重试任务（retry_failed_refunds，内置定时器）兜底重发；
到账结果以微信退款结果通知（on_refund_notify）回写为准并做订单状态对账。
"""
import logging
from datetime import timedelta

from django.db import transaction

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_refund_no
from apps.order.models import TicketOrder, MahuaDispatch
from apps.order.statemachine import transition, can_refund, can_dispute
from apps.refund.models import Refund, Dispute

logger = logging.getLogger('app')


def _get_user_order(order_id, user_id):
    """按订单ID取当前用户的未删订单，不存在抛业务异常。"""
    try:
        return TicketOrder.objects.get(id=order_id, user_id=user_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)


def apply_refund(user_id, order_id, reason=None):
    """申请退款（仅待出票订单，走麻花拦截）。

    业务规则：任何订单不允许改签；「出票中」（待出票，尚未拿到票）订单
    允许退票——先调麻花拦截成功，再对用户退款。
    「待取票」订单走纠纷退票，入口见 dispute_config / apply_dispute。
    """
    order = _get_user_order(order_id, user_id)

    if not can_refund(order):
        raise BizError(
            '仅待出票订单支持退票，出票后订单不支持退票/改签',
            code=ErrorCode.REFUND_NOT_ALLOWED,
        )

    return _intercept_refund(order, reason)


def dispute_config(order_id, user_id):
    """待取票纠纷退票配置：透传麻花纠纷原因 + 本地/上游规则合成可退判定。

    - 本地规则（产品策略）：待取票(30) 且距开场 >= DISPUTE_MIN_MINUTES_BEFORE_SHOW；
    - 上游规则：麻花 dispute/config 的 rules 区间（refundable）+ 每日退票
      时间窗 + disputeMaxCount 次数限制，任一不满足则不可退（fail-closed）；
    - 麻花不可用时不给出可退判定，直接报错，避免用户发起后必然失败。
    """
    order = _get_user_order(order_id, user_id)

    config = {
        'canApplyRefund': False,
        'blockReason': '',
        'reasons': [],
        'feeEstimate': 0,
        'disputeCount': 0,
        'disputeMaxCount': 0,
        'refundable': False,
    }

    if order.status == TicketOrder.STATUS_DISPATCHING:
        # 出票中订单走拦截退款，无纠纷配置
        config['blockReason'] = '出票中订单无需纠纷退票'
        return config
    if not can_dispute(order):
        config['blockReason'] = '距开场不足2小时，不支持退票'
        return config

    data = _mahua_dispute_config(order)
    reasons = list((data or {}).get('reasons') or [])
    fee = _evaluate_refund_fee_fen(data)
    count = int((data or {}).get('disputeCount') or 0)
    max_count = int((data or {}).get('disputeMaxCount') or 0)

    config.update({
        'reasons': reasons,
        'feeEstimate': fee,
        'disputeCount': count,
        'disputeMaxCount': max_count,
    })

    if not reasons:
        config['blockReason'] = '按影院规则当前时段不支持退票'
        return config
    if max_count and count >= max_count:
        config['blockReason'] = '该订单已达最大退票申请次数'
        return config

    rule_block = _rule_block_reason(data, order)
    if rule_block:
        config['blockReason'] = rule_block
        return config

    config['canApplyRefund'] = True
    config['refundable'] = True
    return config


def _mahua_dispute_config(order):
    """调麻花纠纷原因接口（/put/dispute/config），失败抛业务异常（fail-closed）。"""
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE

    client = MahuaClient()
    token = get_token()
    code, data = client.dispute_reason(token, out_id=order.order_ext_no)
    if code != SUCCESS_CODE:
        logger.error('纠纷原因查询失败 order=%s code=%s', order.order_ext_no, code)
        raise BizError('获取退票规则失败，请稍后重试', code=ErrorCode.UP_ERROR)
    return data or {}


def _evaluate_refund_fee_fen(data):
    """麻花预估退票手续费（元）-> 分。"""
    try:
        return int(round(float(data.get('evaluateRefundFee') or 0) * 100))
    except (TypeError, ValueError):
        return 0


def _rule_block_reason(data, order):
    """按麻花 rules 区间/每日时间窗校验当前时点是否可退，不可退返回原因。

    rules[i] = {minMinutes, maxMinutes, refundable, feePerTicket, description}，
    左闭右开 [min, max)，maxMinutes=null 代表无上限。当前时间距开场分钟数
    落入的区间 refundable!=true 即不可退；每日 startTime~endTime 时间窗外
    一律不可退；规则缺失/区间无命中时不放行（fail-closed）。
    """
    from datetime import datetime as dt

    from django.utils import timezone

    cfg = ((data.get('refundRuleConfig') or {}).get('refundRuleConfigPut')) or {}
    rules = cfg.get('rules') or []
    from apps.order.services import order_show_start_at
    start_at = order_show_start_at(order)
    if start_at is None:
        return '场次信息缺失，不支持退票'
    minutes = None
    if start_at is not None:
        # USE_TZ=False 时库内为 naive 时刻，统一升为 aware 再计算
        if timezone.is_naive(start_at):
            start_at = timezone.make_aware(start_at)
        now = timezone.now()
        if timezone.is_naive(now):
            now = timezone.make_aware(now)
        minutes = int((start_at - now).total_seconds() // 60)

    # 每日可发起退票的时间窗（startTime/endTime，例 08:00:00~23:00:00）
    start_time, end_time = cfg.get('startTime'), cfg.get('endTime')
    if start_time and end_time:
        try:
            t0 = dt.strptime(str(start_time), '%H:%M:%S').time()
            t1 = dt.strptime(str(end_time), '%H:%M:%S').time()
            if not (t0 <= timezone.localtime().time() <= t1):
                return f'每日 {str(start_time)[:5]}-{str(end_time)[:5]} 可申请退票，当前时段不可退'
        except ValueError:
            pass  # 时间窗格式异常时不拦，交给区间规则判定

    hit = None
    for rule in rules:
        try:
            lo = int(float(rule.get('minMinutes') or 0))
            hi_raw = rule.get('maxMinutes')
            hi = int(float(hi_raw)) if hi_raw is not None else None
        except (TypeError, ValueError):
            continue
        if minutes >= lo and (hi is None or minutes < hi):
            hit = rule
            break
    if hit is None:
        return '按影院规则当前时段不支持退票'
    if str(hit.get('refundable', '')).lower() != 'true':
        return (hit.get('description') or '').strip() or '按影院规则当前时段不支持退票'
    return None


def apply_dispute(user_id, order_id, reason_code, content=None):
    """用户发起纠纷退票（待取票订单，走麻花 /put/dispute）。"""
    from apps.refund.models import Dispute

    order = _get_user_order(order_id, user_id)

    # 并发守卫（先于时间规则，保证提示语义准确）：同单存在进行中的纠纷/退款单时拒绝重复发起
    if Dispute.objects.filter(
            order_id=order.id,
            status__in=(Dispute.STATUS_STARTED, Dispute.STATUS_PROCESSING)).exists():
        raise BizError('已存在处理中的退票申请，请勿重复提交')
    if Refund.objects.filter(
            order_id=order.id,
            status__in=(Refund.STATUS_ACCEPTED, Refund.STATUS_INTERCEPTING,
                        Refund.STATUS_WAIT_MAHUA, Refund.STATUS_REFUNDING)).exists():
        raise BizError('已存在处理中的退款，请勿重复提交')

    if not can_dispute(order):
        raise BizError(
            '距开场不足2小时或当前状态不支持退票',
            code=ErrorCode.REFUND_NOT_ALLOWED,
        )

    # 发起前用麻花实时规则再校验一次：原因必须来自纠纷原因接口（2026-01-26 起必传）
    data = _mahua_dispute_config(order)
    reasons = list(data.get('reasons') or [])
    if reason_code not in reasons:
        raise BizError('请选择有效的退票原因', code=ErrorCode.PARAM_ERROR)

    return _dispute_refund(order, reason_code, content)


def _intercept_refund(order, reason):
    """未出票：调麻花尝试拦截（/api/movie-server/movie/put/tryIntercept）。

    拦截参数是外部单号 outId（我方订单号）。100300=拦截失败转人工，严禁直接退款。
    """
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE, INTERCEPT_FAIL_CODE

    client = MahuaClient()
    token = get_token()
    code, _ = client.intercept(token, order.order_ext_no)

    if code == INTERCEPT_FAIL_CODE:
        # 拦截失败（转人工）-> 记失败退款单，等人工处理
        Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_INTERCEPT,
            reason=reason,
            refund_amount=order.pay_amount,
            status=Refund.STATUS_FAIL,
        )
        raise BizError('拦截处理中，请稍后查看结果', code=ErrorCode.INTERCEPT_FAILED)
    if code != SUCCESS_CODE:
        logger.error('拦截请求失败 order=%s code=%s', order.order_ext_no, code)
        raise BizError('拦截失败，请稍后重试', code=ErrorCode.UP_ERROR)

    # 拦截成功 -> 订单进入退款链路：出票中(20) -> 退款中(70) -> 已退款(80)
    with transaction.atomic():
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_INTERCEPT,
            reason=reason,
            refund_amount=order.pay_amount,
            status=Refund.STATUS_REFUNDING,
        )
        transition(order, TicketOrder.STATUS_REFUNDING)
        # 回写放单映射：已拦截（后续 drawClose/ticketRefund 回调再收敛）
        MahuaDispatch.objects.filter(order_ext_no=order.order_ext_no).update(
            dispatch_status=MahuaDispatch.STATUS_INTERCEPTED)
        _do_refund_order(order, refund)
    return refund


def _dispute_refund(order, reason_code, content=None):
    """已出票：发起纠纷（/api/movie-server/movie/put/dispute）。

    disputeReason 自 2026-01-26 起必传，取值来自【纠纷原因】接口（由
    apply_dispute 预先校验）；「个人原因申请退票」会扣手续费（响应
    evaluateRefundFee，单位元）。麻花同意退票后由纠纷回调驱动退款。

    outDisputeId 用 DP+订单号+第N次发起 生成，重试/重复提交天然幂等。
    """
    from django.conf import settings as dj_settings
    from apps.refund.models import DisputeMsg
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE

    dispatch = MahuaDispatch.objects.filter(order_ext_no=order.order_ext_no).first()
    if not dispatch or not dispatch.mahua_order_no:
        raise BizError('放单信息缺失')

    attempt = Dispute.objects.filter(order_id=order.id).count() + 1
    out_dispute_id = f'DP{order.order_ext_no}N{attempt}'
    content = content or f'用户申请退票：{reason_code}'

    client = MahuaClient()
    token = get_token()
    code, data = client.dispute_apply(
        token,
        out_id=order.order_ext_no,   # 外部单号（我方订单号）
        reason=reason_code,
        content=content,
        out_dispute_id=out_dispute_id,
        call_back_url=dj_settings.MAHUA.get('CALLBACK_URL') or None,
    )
    if code != SUCCESS_CODE:
        logger.error('发起纠纷失败 order=%s code=%s data=%s', order.order_ext_no, code, data)
        raise BizError('发起纠纷失败，请稍后重试', code=ErrorCode.UP_ERROR)

    data = data or {}
    fee = _evaluate_refund_fee_fen(data)

    # 纠纷已受理：待取票(30) -> 纠纷中(90)，等纠纷回调驱动后续
    with transaction.atomic():
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_DISPUTE,
            reason=reason_code,
            fee=fee,
            refund_amount=max(order.pay_amount - fee, 0),
            status=Refund.STATUS_WAIT_MAHUA,
        )
        dispute = Dispute.objects.create(
            order_id=order.id,
            refund_id=refund.id,
            up_dispute_no=str(data.get('disputeId') or ''),
            reason_code=reason_code,
            reason_text=content,
            status=Dispute.STATUS_STARTED,
        )
        DisputeMsg.objects.create(
            dispute_id=dispute.id, from_role=DisputeMsg.ROLE_USER, content=content)
        transition(order, TicketOrder.STATUS_DISPUTE)

    logger.info(
        '纠纷已发起 order=%s disputeId=%s 预估手续费=%s分',
        order.order_ext_no, dispute.up_dispute_no, fee)
    return refund


def refund_retry_once(order, refund):
    """对单笔退款单发起/重发微信退款（退款受理的最小单元，可被重试任务复用）。

    - 受理成功：记「退款中」+ 回填微信退款单号，到账由退款结果通知确认；
    - 骨架降级（未配置证书）：直接记「已到账」并回冲支付状态，联调链路走通；
    - 被微信拒绝：抛 BizError，调用方决定回滚/重试。
    """
    from apps.pay.services import wx_refund

    if isinstance(refund, int):
        refund = Refund.objects.get(id=refund)
    _accepted, wx_no = wx_refund(order, refund)
    if wx_no:
        refund.wx_refund_no = wx_no
        refund.status = Refund.STATUS_REFUNDING
        refund.save(update_fields=['wx_refund_no', 'status', 'updated_at'])
    else:
        refund.status = Refund.STATUS_ARRIVED
        refund.save(update_fields=['status', 'updated_at'])
        TicketOrder.objects.filter(id=order.id).update(
            pay_status=TicketOrder.PAY_REFUNDED)
    return refund


def _do_refund_order(order, refund):
    """执行退款：先发起微信退款，受理后订单 -> 已退款(80) + 支付状态回冲。

    - 真实退款（配置了商户证书）：被微信拒绝时抛 BizError，调用方事务回滚，
      订单停在「退款中(70)」待人工/重试；受理成功退款单记「退款中」，
      到账由微信退款结果通知（on_refund_notify）确认。
    - 骨架降级（未配置证书）：不真打款，退款单直接标记到账，联调链路照常走通。
    """
    if order.status != TicketOrder.STATUS_REFUNDING:
        transition(order, TicketOrder.STATUS_REFUNDING)

    if refund is not None:
        refund_retry_once(order, refund)

    transition(order, TicketOrder.STATUS_REFUNDED)
    TicketOrder.objects.filter(id=order.id).update(pay_status=TicketOrder.PAY_REFUNDED)


def auto_refund_dispatch_fail(order):
    """出票失败自动全额退款：出票失败(60) -> 退款中(70)，到账后 -> 已退款(80)。

    幂等：同单已存在「出票失败自动退」退款单时直接跳过，不重复退款——
    查询（/put/query 轮询）与回调（drawClose）双路径都可能触发本方法，
    行锁 + 状态前置检查之外再加一层退款单存在性防线。

    退款单先落库提交、再在事务外发起微信退款：受理失败退款单记 FAIL，
    由退款重试任务（retry_failed_refunds）兜底重发——避免微信拒绝时把退款单
    连同状态迁移一起回滚，订单卡死在「出票失败」且无退款单可重试。
    """
    with transaction.atomic():
        if Refund.objects.filter(
                order_id=order.id, type=Refund.TYPE_DISPATCH_FAIL).exists():
            logger.warning('出票失败退款单已存在，跳过重复退款 order=%s', order.order_ext_no)
            return None
        if order.status == TicketOrder.STATUS_DISPATCH_FAIL:
            transition(order, TicketOrder.STATUS_REFUNDING)
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_DISPATCH_FAIL,
            refund_amount=order.pay_amount,
            status=Refund.STATUS_REFUNDING,
        )
    _settle_refund_to_user(order, refund)
    return refund


def refund_by_up_ticket_refund(order):
    """麻花已退票（ticketRefund 回调）：票款已退回我方账户，对用户原路退款。

    纠纷中(90)/退款中(70) -> 退款中(70)，微信退款到账后 -> 已退款(80)
    （真实链路由 on_refund_notify 回写迁移；骨架降级直接到账即迁移）。
    回调可能先于我方退款单到达，退款单不存在则补建；同单已有进行中/到账的
    同类退款单则复用（微信按 out_refund_no 幂等，不会重复打款）。
    微信退款被拒时退款单记 FAIL 交重试任务，不向上抛异常——
    避免麻花因回调处理失败在重推周期内无限重推。
    """
    if order.status == TicketOrder.STATUS_REFUNDED:
        # 已退款：查到账退款单即认为闭环完成（幂等，防重复打款）
        return Refund.objects.filter(
            order_id=order.id, type=Refund.TYPE_UP_REFUND,
        ).exclude(status=Refund.STATUS_FAIL).first()

    with transaction.atomic():
        # 复用最近一笔未到账的同类退款单（FAIL 的重置重发，微信按 out_refund_no 幂等）
        refund = Refund.objects.filter(
            order_id=order.id, type=Refund.TYPE_UP_REFUND,
        ).exclude(status=Refund.STATUS_ARRIVED).order_by('-id').first()
        if refund is None:
            refund = Refund.objects.create(
                refund_ext_no=gen_refund_no(),
                order_id=order.id,
                type=Refund.TYPE_UP_REFUND,
                reason='麻花已退票，原路退款',
                refund_amount=order.pay_amount,
                status=Refund.STATUS_REFUNDING,
            )
        if order.status == TicketOrder.STATUS_DISPUTE:
            transition(order, TicketOrder.STATUS_REFUNDING)

    _settle_refund_to_user(order, refund)
    return refund


def _settle_refund_to_user(order, refund):
    """事务外发起微信退款并收敛订单终态（供自动退款链路复用）。

    - 受理成功：退款单记「退款中」，到账由 on_refund_notify 迁移订单；
    - 骨架降级（未配置证书）：直接到账，订单迁移「已退款」+ 支付状态回冲；
    - 被微信拒绝：退款单记 FAIL，由退款重试任务兜底，不向上抛。
    """
    try:
        refund_retry_once(order, refund)
    except Exception as exc:  # noqa: BLE001 受理失败：记 FAIL 交重试任务
        refund.status = Refund.STATUS_FAIL
        refund.save(update_fields=['status', 'updated_at'])
        logger.error('微信退款发起失败（待重试） refund=%s err=%s',
                     refund.refund_ext_no, exc)
        return refund
    if refund.status == Refund.STATUS_ARRIVED:
        if order.status == TicketOrder.STATUS_REFUNDING:
            transition(order, TicketOrder.STATUS_REFUNDED)
        TicketOrder.objects.filter(id=order.id).update(
            pay_status=TicketOrder.PAY_REFUNDED)
    return refund


# 可自动重试的退款类型：出票失败自动退 / 关单竞态退 / 上游退票退。
# 拦截退款(1)失败是「转人工」语义，严禁自动重试；纠纷退款(2)失败由麻花重推回调驱动。
RETRYABLE_REFUND_TYPES = (
    Refund.TYPE_DISPATCH_FAIL, Refund.TYPE_PAY_AFTER_CLOSE, Refund.TYPE_UP_REFUND,
)


def reconcile_refunding_refunds(limit=100, grace_seconds=60):
    """退款中对账兜底（内置定时器调用）：主动查微信退款结果并收敛订单。

    退款到账依赖微信退款结果通知（/pay/refund-notify）回写；通知丢失/推送
    失败时订单会停留「退款中(70)」而钱实际已到账。此任务对「退款中」的退款单
    调微信「查询退款」接口（/pay/refundquery）对账：
        SUCCESS / CHANGE      -> 到账：退款单记已到账，订单 -> 已退款(80)；
        FAIL / REFUNDCLOSE    -> 失败：退款单记 FAIL，交退款重试任务重发；
        PROCESSING / 查询失败  -> 跳过，等下一轮。
    受理后 grace_seconds 内的新退款单跳过，给通知回写留时间，减少无谓查询。
    幂等：与退款通知共用 _reconcile_order_on_refund，重复收敛无副作用。
    """
    from django.utils import timezone
    from apps.pay.services import wx_refund_query, _reconcile_order_on_refund

    deadline = timezone.now() - timedelta(seconds=grace_seconds)
    refunds = Refund.objects.filter(
        status=Refund.STATUS_REFUNDING, updated_at__lt=deadline,
    ).order_by('updated_at')[:limit]

    res = {'refunding': 0, 'arrived': 0, 'failed': 0, 'processing': 0}
    for refund in refunds:
        res['refunding'] += 1
        order = TicketOrder.objects.filter(id=refund.order_id).first()
        if not order:
            continue
        try:
            status = wx_refund_query(order, refund)
        except Exception as exc:  # noqa: BLE001 单笔异常不影响整批
            logger.warning('退款对账查询异常 refund=%s err=%s', refund.refund_ext_no, exc)
            continue
        if not status:
            continue
        if status in ('SUCCESS', 'CHANGE'):
            refund.status = Refund.STATUS_ARRIVED
            refund.save(update_fields=['status', 'updated_at'])
            _reconcile_order_on_refund(refund, arrived=True)
            logger.info('退款对账：已到账（通知未回写的兜底收敛） refund=%s', refund.refund_ext_no)
            res['arrived'] += 1
        elif status in ('FAIL', 'REFUNDCLOSE'):
            refund.status = Refund.STATUS_FAIL
            refund.save(update_fields=['status', 'updated_at'])
            _reconcile_order_on_refund(refund, arrived=False)
            logger.warning('退款对账：微信退款失败 refund=%s status=%s',
                           refund.refund_ext_no, status)
            res['failed'] += 1
        else:
            res['processing'] += 1
    return res


def refund_retry_max_times():
    """退款失败最大重试次数（settings.REFUND_RETRY_MAX_TIMES，默认 5），0 = 不限制。"""
    from django.conf import settings as dj_settings
    return int(getattr(dj_settings, 'REFUND_RETRY_MAX_TIMES', 5) or 0)


def retry_failed_refunds(limit=50):
    """重试失败的微信退款（内置定时器每 5 分钟扫描一次）。

    退款单 FAIL 且类型可重试、重试次数未达上限（REFUND_RETRY_MAX_TIMES，默认 5）
    -> 重新发起微信退款；每次扫描无论成败都递增 retry_count，达上限后停止自动
    重试，退款单停留 FAIL 状态待人工处理（避免对微信接口无限重试）。

    受理成功即回到「退款中」等通知到账；骨架降级（未配置证书）受理即到账，
    此时微信不会发退款结果通知，需在此主动收敛订单到「已退款」，否则订单
    会永久卡在「退款中(70)」。
    """
    max_times = refund_retry_max_times()
    qs = Refund.objects.filter(
        status=Refund.STATUS_FAIL, type__in=RETRYABLE_REFUND_TYPES,
    ).order_by('updated_at')
    if max_times > 0:
        qs = qs.filter(retry_count__lt=max_times)
    refunds = qs[:limit]

    ok_cnt = fail_cnt = 0
    for refund in refunds:
        # 先递增再发起：无论微信受理与否都消耗一次重试机会
        refund.retry_count += 1
        refund.save(update_fields=['retry_count', 'updated_at'])
        order = TicketOrder.objects.filter(id=refund.order_id).first()
        if order is None:
            continue
        try:
            with transaction.atomic():
                refund_retry_once(order, refund)
            # 骨架降级（未配置证书）受理即到账，微信不发退款结果通知，
            # 主动收敛订单到「已退款」；真实链路退款单仍在「退款中」，
            # 到账由 on_refund_notify 回写（此处 arrived=False 不会误迁）。
            if refund.status == Refund.STATUS_ARRIVED:
                from apps.pay.services import _reconcile_order_on_refund
                _reconcile_order_on_refund(refund, arrived=True)
            ok_cnt += 1
            logger.info('退款重试已受理 refund=%s 第%s次', refund.refund_ext_no, refund.retry_count)
        except Exception as exc:  # noqa: BLE001 单笔失败不影响整批，下轮再试
            fail_cnt += 1
            logger.warning('退款重试失败 refund=%s 第%s/%s次 err=%s',
                           refund.refund_ext_no, refund.retry_count, max_times, exc)
    return {'retried': len(refunds), 'ok': ok_cnt, 'fail': fail_cnt}


# 麻花纠纷回调事件（docs/mahua-api/00-通用说明-02-回调接口纠纷.md）→ 我方动作
# 1005 接单同意退票 / 1006 管理同意退票 => 同意退款
DISPUTE_EVENT_AGREE = {'1005', '1006'}
# 1001 取消纠纷 / 1003 管理取消纠纷 / 1007 确认收货后管理取消纠纷 => 取消
DISPUTE_EVENT_CANCEL = {'1001', '1003', '1007'}
# 1002 接单主动认责 / 1004 管理判接单责任 / 1008 确认收货后管理判接单责任 => 完结(不退款)
DISPUTE_EVENT_REJECT = {'1002', '1004', '1008'}


def on_dispute_callback(payload):
    """纠纷回调：更新纠纷单，按事件驱动订单状态（字段映射 §6.2）。

    回调里 disputeId=麻花纠纷单号（发起纠纷响应中的 disputeId，已存 up_dispute_no）。
    """
    dispute_no = str(payload.get('disputeId') or '')
    dispute = Dispute.objects.filter(up_dispute_no=dispute_no).first() if dispute_no else None
    if dispute is None:
        logger.warning('纠纷回调找不到纠纷单 disputeId=%s payload=%s',
                       dispute_no, payload)
        return

    result = payload.get('resultOut') or {}
    event = str(result.get('resultEvent', ''))
    order = TicketOrder.objects.get(id=dispute.order_id)
    refund = Refund.objects.filter(id=dispute.refund_id).first()

    if event in DISPUTE_EVENT_AGREE:
        # 同意退票：纠纷中(90) -> 退款中(70) -> 已退款(80)
        dispute.status = Dispute.STATUS_AGREED
        dispute.save(update_fields=['status', 'updated_at'])
        _do_refund_order(order, refund)
    elif event in DISPUTE_EVENT_CANCEL:
        # 取消纠纷：纠纷中(90) -> 待取票(30)，退款单作废
        dispute.status = Dispute.STATUS_CANCELLED
        dispute.save(update_fields=['status', 'updated_at'])
        if refund and refund.status not in (Refund.STATUS_ARRIVED, Refund.STATUS_FAIL):
            refund.status = Refund.STATUS_FAIL
            refund.save(update_fields=['status', 'updated_at'])
        if order.status == TicketOrder.STATUS_DISPUTE:
            transition(order, TicketOrder.STATUS_WAIT_PICK)
    elif event in DISPUTE_EVENT_REJECT:
        # 平台判责/完结但不退款：纠纷中(90) -> 待取票(30)
        dispute.status = Dispute.STATUS_DONE
        dispute.save(update_fields=['status', 'updated_at'])
        if refund and refund.status not in (Refund.STATUS_ARRIVED, Refund.STATUS_FAIL):
            refund.status = Refund.STATUS_FAIL
            refund.save(update_fields=['status', 'updated_at'])
        if order.status == TicketOrder.STATUS_DISPUTE:
            transition(order, TicketOrder.STATUS_WAIT_PICK)
    else:
        logger.warning('纠纷回调未知事件 disputeId=%s event=%s', dispute_no, event)

    return dispute
