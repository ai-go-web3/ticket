"""upadapter 回调视图：麻花订单/纠纷/电影/排片回调入口（无需鉴权）。

麻花对所有回调约定固定应答：{"rtnCode":"000000","rtnMsg":"成功!"}。
收到该应答即认为接入方处理完成，不再重试；否则按 15s/15s/30s/3m/…/6h（总计约 24h）
反复重推。因此这里必须返回裸 JSON 的 rtnCode 协议，绝不能套用统一响应包 {code,msg,data}。
"""
import logging
import uuid

from django.db import IntegrityError
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny

from apps.upadapter.models import MahuaCallbackLog
from apps.upadapter.client import on_order_callback

logger = logging.getLogger('app')


def mahua_ack(success=True, msg=None):
    """麻花回调应答：success -> rtnCode 000000，否则非成功码触发重试。

    用 ensure_ascii=False 输出裸 UTF-8，与文档示例 {"rtnMsg":"成功!"} 逐字节一致，
    避免麻花侧做朴素字符串匹配时因 \\u 转义判失败而无限重推。
    """
    body = {'rtnCode': '000000', 'rtnMsg': msg or '成功!'} if success \
        else {'rtnCode': '999999', 'rtnMsg': msg or '处理失败'}
    return JsonResponse(body, json_dumps_params={'ensure_ascii': False})


def _invalid(biz_type, payload, msg):
    """报文畸形（缺必填字段）：无法归属、重试也救不了 -> 落异常日志后直接确认止血。"""
    logger.error('麻花%s回调报文异常(%s)：%s', biz_type, msg, payload)
    try:
        MahuaCallbackLog.objects.create(
            biz_type=biz_type,
            dedup_key=f'{biz_type}:invalid:{uuid.uuid4().hex}',
            payload=payload, handle_status=2, error_msg=msg,
        )
    except IntegrityError:
        pass
    return mahua_ack()  # 确认停止重试，异常靠日志/落库记录排查


def _process(biz_type, dedup_key, payload, handler):
    """统一回调处理：幂等 + rtnCode 协议应答。

    - 已成功处理过 -> 直接确认（止血，不再重复处理）
    - 存在但未成功 -> 允许重处理（上次失败，麻花重推时自愈）
    - 处理抛异常 -> 记失败 + 返非成功码（给瞬态问题重试机会）
    - 处理成功 -> 记成功 + 返 000000
    """
    existing = MahuaCallbackLog.objects.filter(dedup_key=dedup_key).first()
    if existing and existing.handle_status == 1:
        return mahua_ack()  # 已处理完成
    if not existing:
        try:
            MahuaCallbackLog.objects.create(
                biz_type=biz_type, dedup_key=dedup_key, payload=payload, handle_status=0,
            )
        except IntegrityError:
            return mahua_ack()  # 并发下的重复插入，视作已接收

    try:
        handler(payload)
    except Exception as exc:  # noqa: BLE001 - 回调兜底，任何异常都记录并按协议返失败码
        logger.exception('麻花%s回调处理失败 key=%s', biz_type, dedup_key)
        MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(
            handle_status=2, error_msg=str(exc)[:512])
        return mahua_ack(False, msg='处理失败')

    MahuaCallbackLog.objects.filter(dedup_key=dedup_key).update(
        handle_status=1, error_msg=None)
    return mahua_ack()


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def order_callback(request):
    """麻花订单回调（出票结果，字段映射 §6.1）。"""
    payload = request.data
    out_id = payload.get('outId')
    if not out_id:
        return _invalid('order', payload, '缺少 outId')

    dedup_key = f"order:{out_id}:{payload.get('status')}"
    return _process('order', dedup_key, payload, on_order_callback)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def dispute_callback(request):
    """麻花纠纷回调（字段映射 §6.2）。"""
    payload = request.data
    dispute_id = payload.get('disputeId') or payload.get('outDisputeId')
    if not dispute_id:
        return _invalid('dispute', payload, '缺少 disputeId')

    event = (payload.get('resultOut') or {}).get('resultEvent', payload.get('tag'))
    dedup_key = f"dispute:{dispute_id}:{event}"

    from apps.refund.services import on_dispute_callback
    return _process('dispute', dedup_key, payload, on_dispute_callback)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def film_callback(request):
    """麻花电影增量回调（字段映射 §6.3）。"""
    payload = request.data
    film_id = payload.get('id')
    if not film_id:
        return _invalid('movie', payload, '缺少 id')

    dedup_key = f"film:{film_id}"

    from apps.catalog.services import upsert_movie_from_callback
    return _process('movie', dedup_key, payload, upsert_movie_from_callback)


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def schedule_callback(request):
    """麻花排片批量回调（字段映射 §6.4）。"""
    payload = request.data
    cinema_id = payload.get('cinemaId')
    if not cinema_id:
        return _invalid('schedule', payload, '缺少 cinemaId')

    dedup_key = (
        f"schedule:{cinema_id}:"
        f"{len(payload.get('delList') or [])}-"
        f"{len(payload.get('updList') or [])}-"
        f"{len(payload.get('addList') or [])}"
    )

    from apps.catalog.services import sync_schedule_callback
    return _process('schedule', dedup_key, payload, sync_schedule_callback)
