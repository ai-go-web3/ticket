"""退款服务：拦截取消 / 纠纷退票 / 出票失败自动退 / 纠纷回调。"""
import logging

from django.db import transaction

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_refund_no
from apps.order.models import TicketOrder
from apps.order.statemachine import transition, can_refund
from apps.refund.models import Refund, Dispute

logger = logging.getLogger('app')


def apply_refund(user_id, order_id, reason=None):
    """申请退款（未出票走拦截；已出票走纠纷）。

    顺序：先拿麻花拦截/纠纷成功，再对用户退款。
    """
    try:
        order = TicketOrder.objects.get(id=order_id, user_id=user_id, deleted=0)
    except TicketOrder.DoesNotExist:
        raise BizError('订单不存在', code=ErrorCode.ORDER_NOT_EXIST)

    if not can_refund(order):
        raise BizError('当前状态不可退款', code=ErrorCode.REFUND_NOT_ALLOWED)

    if order.status == TicketOrder.STATUS_DISPATCHING:
        # 未出票 -> 尝试拦截
        return _intercept_refund(order, reason)
    else:
        # 已出票 -> 发起纠纷
        return _dispute_refund(order, reason)


def _intercept_refund(order, reason):
    """拦截取消。"""
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient
    from apps.order.models import MahuaDispatch

    # 调麻花尝试拦截
    client = MahuaClient()
    token = get_token()
    dispatch = MahuaDispatch.objects.filter(order_ext_no=order.order_ext_no).first()
    if not dispatch or not dispatch.mahua_order_no:
        raise BizError('放单信息缺失，无法拦截')

    resp = client.intercept(token, dispatch.mahua_order_no)
    if resp.get('code') == '100300':
        # 拦截失败 -> 转人工，严禁直接退款
        refund = Refund.objects.create(
            refund_ext_no=gen_refund_no(),
            order_id=order.id,
            type=Refund.TYPE_INTERCEPT,
            reason=reason,
            refund_amount=order.pay_amount,
            status=Refund.STATUS_FAIL,
        )
        raise BizError('拦截处理中，请稍后查看结果', code=ErrorCode.INTERCEPT_FAILED)

    # 拦截成功 -> 退款
    refund = Refund.objects.create(
        refund_ext_no=gen_refund_no(),
        order_id=order.id,
        type=Refund.TYPE_INTERCEPT,
        reason=reason,
        refund_amount=order.pay_amount,
        mahua_refund_no=resp.get('refundNo'),
        status=Refund.STATUS_ARRIVED,
    )
    _do_refund_order(order, refund)
    return refund


def _dispute_refund(order, reason):
    """发起纠纷（个人原因退票）。"""
    from apps.upadapter.token import get_token
    from apps.upadapter.mahua import MahuaClient
    from apps.order.models import MahuaDispatch

    client = MahuaClient()
    token = get_token()
    dispatch = MahuaDispatch.objects.filter(order_ext_no=order.order_ext_no).first()
    if not dispatch or not dispatch.mahua_order_no:
        raise BizError('放单信息缺失')

    reason_code = 'PERSONAL'  # 骨架：个人原因
    resp = client.dispute_apply(token, dispatch.mahua_order_no, reason_code)

    refund = Refund.objects.create(
        refund_ext_no=gen_refund_no(),
        order_id=order.id,
        type=Refund.TYPE_DISPUTE,
        reason=reason,
        refund_amount=order.pay_amount,
        status=Refund.STATUS_WAIT_MAHUA,
    )
    dispute = Dispute.objects.create(
        order_id=order.id,
        refund_id=refund.id,
        up_dispute_no=resp.get('disputeNo'),
        reason_code=reason_code,
        reason_text=reason,
        status=Dispute.STATUS_STARTED,
    )
    transition(order, TicketOrder.STATUS_DISPUTE)
    return refund


def _do_refund_order(order, refund):
    """执行退款：状态迁移 + 标记已退。"""
    transition(order, TicketOrder.STATUS_REFUNDED)
    TicketOrder.objects.filter(id=order.id).update(pay_status=TicketOrder.PAY_REFUNDED)


def auto_refund_dispatch_fail(order):
    """出票失败自动全额退款。"""
    refund = Refund.objects.create(
        refund_ext_no=gen_refund_no(),
        order_id=order.id,
        type=Refund.TYPE_DISPATCH_FAIL,
        refund_amount=order.pay_amount,
        status=Refund.STATUS_ARRIVED,
    )
    transition(order, TicketOrder.STATUS_REFUNDED)
    TicketOrder.objects.filter(id=order.id).update(pay_status=TicketOrder.PAY_REFUNDED)
    return refund


# 麻花纠纷回调事件（字段映射 §6.2）→ 我方动作
# 1005 接单同意退票 / 1006 管理同意退票 => 同意退款
DISPUTE_EVENT_AGREE = {'1005', '1006'}
# 1001 取消纠纷 / 1003 管理取消纠纷 / 1007 确认收货后管理取消纠纷 => 取消
DISPUTE_EVENT_CANCEL = {'1001', '1003', '1007'}
# 1002 接单主动认责 / 1004 管理判接单责任 / 1008 确认收货后管理判接单责任 => 完结(不退款)
DISPUTE_EVENT_REJECT = {'1002', '1004', '1008'}


def on_dispute_callback(payload):
    """纠纷回调：更新纠纷状态，同意则退款（字段映射 §6.2）。"""
    dispute_no = payload.get('disputeId') or payload.get('outDisputeId')
    try:
        dispute = Dispute.objects.get(up_dispute_no=dispute_no)
    except Dispute.DoesNotExist:
        # 也可能用 outDisputeId 存（发起纠纷时 outDisputeId 是外部号，disputeId 是麻花号）
        dispute = Dispute.objects.filter(up_dispute_no=payload.get('disputeId')).first()
        if not dispute:
            return

    result = payload.get('resultOut') or {}
    event = str(result.get('resultEvent', ''))

    if event in DISPUTE_EVENT_AGREE:
        # 同意退票：退款回冲 + 扣回佣金
        dispute.status = Dispute.STATUS_AGREED
        dispute.save(update_fields=['status', 'updated_at'])
        order = TicketOrder.objects.get(id=dispute.order_id)
        _do_refund_order(order, dispute.refund_id)
    elif event in DISPUTE_EVENT_CANCEL:
        # 取消纠纷：恢复订单为待取票
        dispute.status = Dispute.STATUS_CANCELLED
        dispute.save(update_fields=['status', 'updated_at'])
        order = TicketOrder.objects.get(id=dispute.order_id)
        transition(order, TicketOrder.STATUS_WAIT_PICK)
    elif event in DISPUTE_EVENT_REJECT:
        # 平台判责/完结但不退款：恢复订单待取票
        dispute.status = Dispute.STATUS_DONE
        dispute.save(update_fields=['status', 'updated_at'])
        order = TicketOrder.objects.get(id=dispute.order_id)
        transition(order, TicketOrder.STATUS_WAIT_PICK)
