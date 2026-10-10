"""order 视图。"""
import logging
from datetime import timedelta

from django.db.models import Q
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework import serializers
from django.utils import timezone

from apps.common.response import ok, fail, BizError
from apps.order import services
from apps.order.models import TicketOrder, Ticket
from apps.order.serializers import OrderSerializer, TicketSerializer

logger = logging.getLogger('app')


# 「我的」页四态入口 -> TicketOrder.status 集合映射
ORDER_TAB_STATUS = {
    'paying': [TicketOrder.STATUS_PAYING],                                   # 待付款
    'ticketing': [TicketOrder.STATUS_DISPATCHING],                           # 出票中
    'issued': [TicketOrder.STATUS_WAIT_PICK, TicketOrder.STATUS_SCREENED,
               TicketOrder.STATUS_DONE],                                      # 已出票(含散场已放映)
    'refunded': [TicketOrder.STATUS_REFUNDING, TicketOrder.STATUS_REFUNDED], # 已退款
}


class CreateOrderSerializer(serializers.Serializer):
    scheduleId = serializers.IntegerField()
    seats = serializers.ListField(child=serializers.DictField())
    mobile = serializers.CharField(required=False, allow_blank=True)
    discountAmount = serializers.IntegerField(required=False, default=0)
    # 购票方式：tehui 特惠（放单不传 model）/ kuai 快速（放单 model=1 快速通道）
    buyMode = serializers.CharField(required=False, default='tehui')
    # 观影代金券码（可空）：建单时校验并占用，支付成功核销，关单/退款回滚
    voucherNo = serializers.CharField(required=False, allow_blank=True, default='')


@api_view(['POST'])
def create_order(request):
    """建单。"""
    ser = CreateOrderSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    order = services.create_order(request.user_id, ser.validated_data)
    return ok(OrderSerializer(order).data)


@api_view(['GET'])
def order_detail(request, order_id):
    """订单详情。"""
    order = services.query_order(order_id, user_id=request.user_id)
    # 看过即灭：进入详情即标记该单通知已读（清退款到账"新"角标），条件更新幂等
    if not order.is_read:
        TicketOrder.objects.filter(id=order.id, is_read=0).update(is_read=1)
        order.is_read = 1
    data = OrderSerializer(order).data
    tickets = Ticket.objects.filter(order_id=order_id)
    data['tickets'] = TicketSerializer(tickets, many=True).data
    return ok(data)


@api_view(['GET'])
def my_orders(request):
    """我的订单列表。支持 tab（四态分组）/ status（单一状态）/ months（近 N 月）过滤。

    - months：按下单时间只返回近 N 个自然月，入参钳制在 1~3（默认 3，即上限全量）；
    - 排序固定按下单时间倒序（created_at DESC），前端无需再排；
    - 待付款已超时的单先惰性关闭再返回（定时器兜底前的即时收敛）。
    """
    qs = TicketOrder.objects.filter(user_id=request.user_id, deleted=0)
    tab = request.query_params.get('tab')
    status = request.query_params.get('status')
    if tab and tab in ORDER_TAB_STATUS:
        qs = qs.filter(status__in=ORDER_TAB_STATUS[tab])
    elif status:
        qs = qs.filter(status=status)
    try:
        months = int(request.query_params.get('months', 3))
    except (TypeError, ValueError):
        months = 3
    qs = qs.filter(created_at__gte=_months_ago(max(1, min(months, 3))))
    qs = qs.order_by('-created_at')
    for order in qs:
        if services.close_if_expired(order):
            order.refresh_from_db()
    return ok(OrderSerializer(qs, many=True).data)


def _months_ago(months):
    """当前时刻往前推 months 个自然月（月末日期自动钳制，如 3/31 - 1月 = 2/28）。"""
    import calendar
    now = timezone.now()
    month, year = now.month - months, now.year
    while month <= 0:
        month += 12
        year -= 1
    day = min(now.day, calendar.monthrange(year, month)[1])
    return now.replace(year=year, month=month, day=day)


@api_view(['GET'])
def order_count(request):
    """「我的」页订单分级计数（角标 + 待办数）。

    分级口径（配合订单提醒重设计）：
      - paying        待付款(未超时)  -> 强提醒红色数字
      - ticketing     出票中          -> 进度灰点（不催）
      - wait_pick     待取票          -> 强提醒红色数字
      - refunded_unread 退款到账未读  -> 弱提醒"新"字（进详情即清）
      - todo          待办数 = paying + wait_pick -> tabBar「我的」角标
    历史单（已完成 / 已读退款）不计入任何角标。待付款排除已超时未关单的陈旧单。
    """
    now = timezone.now()
    deadline = now - timedelta(seconds=services.pay_timeout_seconds())
    base = TicketOrder.objects.filter(user_id=request.user_id, deleted=0)
    paying = base.filter(status=TicketOrder.STATUS_PAYING, created_at__gte=deadline).count()
    ticketing = base.filter(status=TicketOrder.STATUS_DISPATCHING).count()
    wait_pick = base.filter(status=TicketOrder.STATUS_WAIT_PICK).count()
    refunded_unread = base.filter(status=TicketOrder.STATUS_REFUNDED, is_read=0).count()
    return ok({
        'paying': paying,
        'ticketing': ticketing,
        'wait_pick': wait_pick,
        'refunded_unread': refunded_unread,
        'todo': paying + wait_pick,
    })


@api_view(['GET'])
def order_todo(request):
    """「我的」页待办卡：待付款(未超时) + 待取票，按紧急度排序，最多 5 条。

    排序：待付款优先（越接近超时越靠前，按 created_at 升序），其后待取票（按开场时间升序）。
    返回项复用 OrderSerializer，前端取 payRemainSeconds 做倒计时、showTime/movieName 做展示。
    """
    now = timezone.now()
    deadline = now - timedelta(seconds=services.pay_timeout_seconds())
    qs = TicketOrder.objects.filter(user_id=request.user_id, deleted=0).filter(
        Q(status=TicketOrder.STATUS_PAYING, created_at__gte=deadline)
        | Q(status=TicketOrder.STATUS_WAIT_PICK)
    )
    items = list(qs)

    def _key(o):
        if o.status == TicketOrder.STATUS_PAYING:
            return (0, o.created_at)
        return (1, o.show_start_at or o.created_at)

    items.sort(key=_key)
    return ok(OrderSerializer(items[:5], many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def finance_daily(request):
    """财务日报（运维）：按日实收/票款/成本/毛利/对账差异。

    鉴权与 catalog/sync_movies 同口径（TASK_TOKEN，fail-closed）：
    header X-Task-Token > query ?token=。query: days(默认7) 或 date(YYYY-MM-DD)。
    """
    import hmac
    from django.conf import settings as dj_settings
    from apps.common.response import ErrorCode

    expected = getattr(dj_settings, 'TASK_TOKEN', '') or ''
    if not expected:
        return fail('服务端未配置 TASK_TOKEN，财务接口已禁用', code=ErrorCode.FORBIDDEN)
    provided = (request.headers.get('X-Task-Token')
                or request.query_params.get('token') or '')
    if not hmac.compare_digest(str(provided), str(expected)):
        return fail('令牌无效', code=ErrorCode.UNAUTHORIZED)

    try:
        days = int(request.query_params.get('days', 7))
    except (TypeError, ValueError):
        days = 7
    from apps.order.reporting import daily_report
    return ok(daily_report(days=max(1, min(days, 90)),
                            date=request.query_params.get('date')))


@api_view(['POST'])
def cancel_order(request, order_id):
    """取消订单（待付款 -> 已关闭）。"""
    order = services.query_order(order_id, user_id=request.user_id)
    if order.status != TicketOrder.STATUS_PAYING:
        raise BizError('当前状态不可取消')
    from apps.order.statemachine import transition
    transition(order, TicketOrder.STATUS_CLOSED)
    TicketOrder.objects.filter(id=order.id).update(close_reason='用户主动取消')
    services.restore_order_voucher(order)
    return ok(None, msg='已取消')
