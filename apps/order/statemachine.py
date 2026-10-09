"""订单状态机。

集中管理订单状态迁移，禁止散落 if-else 改 status。
状态流：待支付(10) → 出票中(20) → 待取票(30) → 已完成(40)
异常分支：已关闭(50) / 出票失败(60) / 退款中(70) → 已退款(80) / 纠纷中(90)
"""
from apps.common.response import BizError, ErrorCode
from apps.order.models import TicketOrder

# 允许的迁移表
TRANSITIONS = {
    TicketOrder.STATUS_PAYING: {
        TicketOrder.STATUS_DISPATCHING,   # 支付成功 -> 出票中
        TicketOrder.STATUS_CLOSED,        # 超时未付 -> 关闭
        TicketOrder.STATUS_DISPATCH_FAIL, # 直接失败
    },
    TicketOrder.STATUS_DISPATCHING: {
        TicketOrder.STATUS_WAIT_PICK,     # 出票成功
        TicketOrder.STATUS_DISPATCH_FAIL, # 出票失败
        TicketOrder.STATUS_REFUNDING,     # 出票中拦截/退款
    },
    TicketOrder.STATUS_WAIT_PICK: {
        TicketOrder.STATUS_DONE,          # 确认收货
        TicketOrder.STATUS_REFUNDING,     # 个人原因退票 -> 纠纷/退款
        TicketOrder.STATUS_DISPUTE,       # 进入纠纷
    },
    TicketOrder.STATUS_DONE: set(),       # 终态
    TicketOrder.STATUS_CLOSED: set(),     # 终态
    TicketOrder.STATUS_DISPATCH_FAIL: {
        TicketOrder.STATUS_REFUNDING,     # 出票失败自动退款
    },
    TicketOrder.STATUS_REFUNDING: {
        TicketOrder.STATUS_REFUNDED,      # 退款到账
    },
    TicketOrder.STATUS_REFUNDED: set(),   # 终态
    TicketOrder.STATUS_DISPUTE: {
        TicketOrder.STATUS_WAIT_PICK,     # 纠纷取消恢复
        TicketOrder.STATUS_REFUNDING,     # 纠纷同意退款
        TicketOrder.STATUS_REFUNDED,      # 麻花已退票回调（ticketRefund）
    },
}


def transition(order, to_status, force=False):
    """执行状态迁移（带乐观锁 version）。"""
    from django.db.models import F

    if not force and to_status not in TRANSITIONS.get(order.status, set()):
        raise BizError(
            f'非法状态迁移: {order.status} -> {to_status}',
            code=ErrorCode.ORDER_STATE_ILLEGAL,
        )

    updated = TicketOrder.objects.filter(
        id=order.id, version=order.version, deleted=0,
    ).update(status=to_status, version=F('version') + 1)

    if not updated:
        raise BizError('订单状态已变更，请刷新重试', code=ErrorCode.ORDER_STATE_ILLEGAL)

    order.status = to_status
    order.version += 1
    return order


def can_refund(order):
    """是否可申请退款。

    业务规则：任何订单不允许改签；仅「待出票」（出票中，尚未拿到票）订单
    允许退票（走麻花拦截）。已出票（待取票）及之后状态一律不可退。
    """
    return order.status == TicketOrder.STATUS_DISPATCHING


def can_dispute(order, now=None):
    """待取票订单是否可发起纠纷退票。

    业务规则：仅「待取票」（已出票）订单，且距开场时间不少于
    DISPUTE_MIN_MINUTES_BEFORE_SHOW（默认 120 分钟，产品策略；麻花侧
    rules 区间可能在发起时进一步收紧，以纠纷原因接口实时校验为准）。
    开场时间取订单快照 show_start_at，缺失时回退排片表。
    """
    from datetime import timedelta

    from django.conf import settings as dj_settings
    from django.utils import timezone

    if order.status != TicketOrder.STATUS_WAIT_PICK:
        return False
    from apps.order.services import order_show_start_at
    start_at = order_show_start_at(order)
    if start_at is None:
        return False
    if now is None:
        now = timezone.now()
    # USE_TZ=False 时库内为 naive 时刻，统一升为 aware 再比较（两种配置都正确）
    if timezone.is_naive(now):
        now = timezone.make_aware(now)
    if timezone.is_naive(start_at):
        start_at = timezone.make_aware(start_at)
    min_minutes = int(getattr(
        dj_settings, 'DISPUTE_MIN_MINUTES_BEFORE_SHOW', 120) or 120)
    return now < start_at - timedelta(minutes=min_minutes)
