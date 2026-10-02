"""退款服务：拦截取消 / 纠纷退票 / 出票失败自动退 / 纠纷回调。

完整状态链路（配合 apps/order/statemachine.py）：
- 待付款(10)  --取消订单--> 已关闭(50)            （apps/order/views.cancel_order）
- 待付款(10)  --支付成功--> 出票中(20)            （apps/order/services.mark_paid）
- 出票中(20)  --出票成功--> 待取票(30) --确认收货--> 已完成(40)
- 出票中(20)  --拦截成功--> 退款中(70) --到账-->   已退款(80)
- 出票中(20)  --出票失败--> 出票失败(60) --自动退--> 退款中(70) -> 已退款(80)
- 待取票(30)  --发起纠纷(/movie/put/dispute)--> 纠纷中(90)   （仅客服/异常场景，用户入口已关闭）
- 纠纷中(90)  --麻花同意退票(1005/1006)--> 退款中(70) -> 已退款(80)
- 纠纷中(90)  --取消纠纷(1001/1003/1007)/判责(1002/1004/1008)--> 待取票(30)
- 纠纷中(90)  --麻花已退票回调(ticketRefund)--> 已退款(80)

退款顺序：先拿麻花拦截/纠纷结果，再对用户退款（未对接微信退款前骨架同步标记到账）。
"""
import logging

from django.db import transaction

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_refund_no
from apps.order.models import TicketOrder, MahuaDispatch
from apps.order.statemachine import transition, can_refund
from apps.refund.models import Refund, Dispute

logger = logging.getLogger('app')


def apply_refund(user_id, order_id, reason=None):
    """申请退款（仅待出票订单，走麻花拦截）。

    业务规则：任何订单不允许改签；仅「出票中」（待出票，尚未拿到票）订单
    允许退票——先调麻花拦截成功，再对用户退款。已出票（待取票/已完成）及
    其他状态一律拒绝。

    _dispute_refund（纠纷退票）不再由用户退款入口触发，仅供客服/异常场景
    手工调用或后续开放。
    """
    try:
        order = TicketOrder.objects.get(id=order_id, user_id=user_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)

    if not can_refund(order):
        raise BizError(
            '仅待出票订单支持退票，出票后订单不支持退票/改签',
            code=ErrorCode.REFUND_NOT_ALLOWED,
        )

    return _intercept_refund(order, reason)


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
        _do_refund_order(order, refund)
    return refund


def _dispute_refund(order, reason):
    """已出票：发起纠纷（/api/movie-server/movie/put/dispute）。

    disputeReason 自 2026-01-26 起必传，取值来自【纠纷原因】接口；
    「个人原因申请退票」会扣手续费（响应 evaluateRefundFee，单位元）。
    麻花同意退票后由纠纷回调驱动退款。
    """
    from django.conf import settings as dj_settings
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE

    dispatch = MahuaDispatch.objects.filter(order_ext_no=order.order_ext_no).first()
    if not dispatch or not dispatch.mahua_order_no:
        raise BizError('放单信息缺失')

    reason_code = '个人原因申请退票'
    content = reason or '用户申请退票'

    client = MahuaClient()
    token = get_token()
    code, data = client.dispute_apply(
        token,
        out_id=order.order_ext_no,   # 外部单号（我方订单号）
        reason=reason_code,
        content=content,
        call_back_url=dj_settings.MAHUA.get('CALLBACK_URL') or None,
    )
    if code != SUCCESS_CODE:
        logger.error('发起纠纷失败 order=%s code=%s data=%s', order.order_ext_no, code, data)
        raise BizError('发起纠纷失败，请稍后重试', code=ErrorCode.UP_ERROR)

    data = data or {}
    fee = int(round(float(data.get('evaluateRefundFee') or 0) * 100))

    # 纠纷已受理：待取票(30) -> 纠纷中(90)，等纠纷回调驱动后续
    with transaction.atomic():
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_DISPUTE,
            reason=reason,
            fee=fee,
            refund_amount=max(order.pay_amount - fee, 0),
            status=Refund.STATUS_WAIT_MAHUA,
        )
        dispute = Dispute.objects.create(
            order_id=order.id,
            refund_id=refund.id,
            up_dispute_no=str(data.get('disputeId') or ''),
            reason_code=reason_code,
            reason_text=reason,
            status=Dispute.STATUS_STARTED,
        )
        transition(order, TicketOrder.STATUS_DISPUTE)

    logger.info(
        '纠纷已发起 order=%s disputeId=%s 预估手续费=%s分',
        order.order_ext_no, dispute.up_dispute_no, fee)
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
        if isinstance(refund, int):
            refund = Refund.objects.get(id=refund)
        from apps.pay.services import wx_refund
        _accepted, wx_no = wx_refund(order, refund)
        if wx_no:
            refund.wx_refund_no = wx_no
            refund.status = Refund.STATUS_REFUNDING
            refund.save(update_fields=['wx_refund_no', 'status', 'updated_at'])
        else:
            refund.status = Refund.STATUS_ARRIVED
            refund.save(update_fields=['status', 'updated_at'])

    transition(order, TicketOrder.STATUS_REFUNDED)
    TicketOrder.objects.filter(id=order.id).update(pay_status=TicketOrder.PAY_REFUNDED)


def auto_refund_dispatch_fail(order):
    """出票失败自动全额退款：出票失败(60) -> 退款中(70) -> 已退款(80)。"""
    with transaction.atomic():
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_DISPATCH_FAIL,
            refund_amount=order.pay_amount,
            status=Refund.STATUS_REFUNDING,
        )
        _do_refund_order(order, refund)
    return refund


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
