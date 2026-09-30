"""upadapter 回调视图：麻花订单/纠纷回调入口（无需鉴权）。"""
import logging

from django.db import IntegrityError
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny

from apps.common.response import ok, BizError
from apps.upadapter.models import MahuaCallbackLog
from apps.upadapter.client import on_order_callback

logger = logging.getLogger('app')


def _dedup(biz_type, dedup_key, payload):
    """回调去重：记录日志 + 幂等。返回是否首次。"""
    try:
        MahuaCallbackLog.objects.create(
            biz_type=biz_type, dedup_key=dedup_key, payload=payload,
            handle_status=0,
        )
        return True
    except IntegrityError:
        MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(handle_status=3)
        return False


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def order_callback(request):
    """麻花订单回调（出票结果，字段映射 §6.1）。"""
    payload = request.data
    out_id = payload.get('outId')
    if not out_id:
        raise BizError('缺少 outId')

    dedup_key = f"order:{out_id}:{payload.get('status')}"
    if not _dedup('order', dedup_key, payload):
        return ok(None, msg='重复回调已忽略')

    on_order_callback(payload)
    MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(handle_status=1)
    # 麻花要求返回固定格式才算处理完成
    return ok(None)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def dispute_callback(request):
    """麻花纠纷回调（字段映射 §6.2）。"""
    payload = request.data
    dispute_id = payload.get('disputeId') or payload.get('outDisputeId')
    if not dispute_id:
        raise BizError('缺少 disputeId')

    event = (payload.get('resultOut') or {}).get('resultEvent', payload.get('tag'))
    dedup_key = f"dispute:{dispute_id}:{event}"
    if not _dedup('dispute', dedup_key, payload):
        return ok(None, msg='重复回调已忽略')

    from apps.refund.services import on_dispute_callback
    on_dispute_callback(payload)
    MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(handle_status=1)
    return ok(None)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def film_callback(request):
    """麻花电影增量回调（字段映射 §6.3）。"""
    payload = request.data
    film_id = payload.get('id')
    if not film_id:
        raise BizError('缺少 id')

    dedup_key = f"film:{film_id}"
    if not _dedup('movie', dedup_key, payload):
        return ok(None, msg='重复回调已忽略')

    from apps.catalog.services import upsert_movie_from_callback
    upsert_movie_from_callback(payload)
    MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(handle_status=1)
    return ok(None)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def schedule_callback(request):
    """麻花排片批量回调（字段映射 §6.4）。"""
    payload = request.data
    cinema_id = payload.get('cinemaId')
    if not cinema_id:
        raise BizError('缺少 cinemaId')

    dedup_key = f"schedule:{cinema_id}:{len(payload.get('delList') or [])}-{len(payload.get('updList') or [])}-{len(payload.get('addList') or [])}"
    if not _dedup('schedule', dedup_key, payload):
        return ok(None, msg='重复回调已忽略')

    from apps.catalog.services import sync_schedule_callback
    sync_schedule_callback(payload)
    MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(handle_status=1)
    return ok(None)
