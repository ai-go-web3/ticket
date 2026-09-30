"""seat 视图：锁座 / 释放锁。"""
import logging

from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok, BizError
from apps.seat import services

logger = logging.getLogger('app')


class LockSeatSerializer(serializers.Serializer):
    scheduleId = serializers.IntegerField()
    seats = serializers.ListField(child=serializers.DictField())


@api_view(['POST'])
def lock(request):
    """锁座。"""
    ser = LockSeatSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    schedule_id = ser.validated_data['scheduleId']
    seats = ser.validated_data['seats']

    lock_token, ttl, seat_names = services.lock_seats(
        request.user_id, schedule_id, seats,
    )
    return ok({'lockToken': lock_token, 'ttlSeconds': ttl, 'seats': seat_names})


@api_view(['POST'])
def release(request):
    """释放锁（下单前主动取消）。"""
    lock_token = request.data.get('lockToken')
    if not lock_token:
        raise BizError('缺少 lockToken')
    services.release_lock(lock_token)
    return ok(None, msg='已释放')
