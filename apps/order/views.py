"""order 视图。"""
import logging

from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework import serializers

from apps.common.response import ok, fail, BizError
from apps.order import services
from apps.order.models import TicketOrder, Ticket
from apps.order.serializers import OrderSerializer, TicketSerializer

logger = logging.getLogger('app')


# 「我的」页四态入口 -> TicketOrder.status 集合映射
ORDER_TAB_STATUS = {
    'paying': [TicketOrder.STATUS_PAYING],                                   # 待付款
    'ticketing': [TicketOrder.STATUS_DISPATCHING],                           # 出票中
    'issued': [TicketOrder.STATUS_WAIT_PICK, TicketOrder.STATUS_DONE],       # 已出票
    'refunded': [TicketOrder.STATUS_REFUNDING, TicketOrder.STATUS_REFUNDED], # 已退款
}


class CreateOrderSerializer(serializers.Serializer):
    scheduleId = serializers.IntegerField()
    seats = serializers.ListField(child=serializers.DictField())
    mobile = serializers.CharField(required=False, allow_blank=True)
    discountAmount = serializers.IntegerField(required=False, default=0)
    # 购票方式：tehui 特惠（放单不传 model）/ kuai 快速（放单 model=1 快速通道）
    buyMode = serializers.CharField(required=False, default='tehui')


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
    data = OrderSerializer(order).data
    tickets = Ticket.objects.filter(order_id=order_id)
    data['tickets'] = TicketSerializer(tickets, many=True).data
    return ok(data)


@api_view(['GET'])
def my_orders(request):
    """我的订单列表。支持 tab（四态分组）或 status（单一状态）过滤。

    待付款已超时的单先惰性关闭再返回（定时器兜底前的即时收敛）。
    """
    qs = TicketOrder.objects.filter(user_id=request.user_id, deleted=0)
    tab = request.query_params.get('tab')
    status = request.query_params.get('status')
    if tab and tab in ORDER_TAB_STATUS:
        qs = qs.filter(status__in=ORDER_TAB_STATUS[tab])
    elif status:
        qs = qs.filter(status=status)
    qs = qs.order_by('-created_at')
    for order in qs:
        if services.close_if_expired(order):
            order.refresh_from_db()
    return ok(OrderSerializer(qs, many=True).data)


@api_view(['GET'])
def order_count(request):
    """我的订单四态计数（供「我的」页角标）。"""
    base = TicketOrder.objects.filter(user_id=request.user_id, deleted=0)
    return ok({
        tab: base.filter(status__in=statuses).count()
        for tab, statuses in ORDER_TAB_STATUS.items()
    })


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
    return ok(None, msg='已取消')
