"""order 视图。"""
import logging

from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok, BizError
from apps.order import services
from apps.order.models import TicketOrder, Ticket
from apps.order.serializers import OrderSerializer, TicketSerializer

logger = logging.getLogger('app')


class CreateOrderSerializer(serializers.Serializer):
    lockToken = serializers.CharField()
    scheduleId = serializers.IntegerField()
    seats = serializers.ListField(child=serializers.DictField())
    mobile = serializers.CharField(required=False, allow_blank=True)
    discountAmount = serializers.IntegerField(required=False, default=0)


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
    """我的订单列表。"""
    status = request.query_params.get('status')
    qs = TicketOrder.objects.filter(user_id=request.user_id, deleted=0)
    if status:
        qs = qs.filter(status=status)
    qs = qs.order_by('-created_at')
    return ok(OrderSerializer(qs, many=True).data)


@api_view(['POST'])
def cancel_order(request, order_id):
    """取消订单（未支付 -> 释放座位）。"""
    order = services.query_order(order_id, user_id=request.user_id)
    if order.status != TicketOrder.STATUS_PAYING:
        raise BizError('当前状态不可取消')
    from apps.order.statemachine import transition
    transition(order, TicketOrder.STATUS_CLOSED)
    from apps.seat.services import release_lock
    if order.lock_token:
        release_lock(order.lock_token)
    return ok(None, msg='已取消')
