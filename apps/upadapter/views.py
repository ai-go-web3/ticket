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

from apps.upadapter.models import MahuaCallbackLog, OcrSession
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


# ============ OCR 图片识别报价（C 端，须登录态）============
# 与回调不同：这两个接口面向已登录用户，走 JWTAuthMiddleware 的 PROTECTED_PREFIXES
# （见 apps/common/middleware.py，已把 '/api/v1/up/ocr' 加入受保护前缀）。
# 故这里用裸 @api_view 即可——无 token 时中间件已返 401，进到视图时 request.user_id 必有效。
from apps.common.response import ok                    # noqa: E402
from apps.upadapter import ocr_services                # noqa: E402


@api_view(['POST'])
def ocr_recognize(request):
    """上传选座截图 → 麻花 OCR 识别 → 匹配在售场次 → 上浮定价，返回识别会话。

    body: {imgBase64, outBizNo?}。status 语义见 OcrSession：1 唯一可下单/2 多候选/3,4 失败。
    成交价为麻花 evaluatePrice 经我方上浮规则加成后的 sellAmountFen（回传展示）。
    """
    img = (request.data.get('imgBase64') or '').strip()
    out_biz_no = request.data.get('outBizNo') or ('ocr_' + uuid.uuid4().hex)
    sess = ocr_services.recognize(request.user_id, out_biz_no, img)
    failed = sess.status in (OcrSession.ST_NOMATCH, OcrSession.ST_FAIL)
    return ok({
        'ocrSessionId': sess.id,
        'status': sess.status,
        'rtnCode': sess.rtn_code,
        'rtnMsg': sess.rtn_msg,
        'errorStage': sess.error_stage,
        'errorMsg': ocr_services.err_msg(sess) if failed else None,
        'film': (sess.raw_response_json or {}).get('film'),
        'cinema': (sess.raw_response_json or {}).get('cinema'),
        'schedule': sess.schedule_json,
        'seats': sess.seats_json,
        'seatCount': sess.seat_count,
        'faceTotalFen': sess.face_total_fen,   # 原价合计（分）
        'sellAmountFen': sess.sell_fen,        # 上浮后售价 = 成交价/应付（分）
        'saveFen': sess.save_fen,              # 省 = 原价 - 上浮后售价
        'candidates': sess.candidates_json,
        'canOrder': sess.status == OcrSession.ST_SUCCESS,
    })


@api_view(['POST'])
def ocr_resolve(request):
    """把识别会话翻译成现成 order/create 可直接消费的入参（座位补全 + 上浮成交价）。

    body: {ocrSessionId, chosenIndex?}。多候选时传 chosenIndex 选定场次。
    前端拿到 {scheduleId, seats, discountAmount} 后直接 POST /api/v1/order/create。
    """
    from apps.common.response import BizError, ErrorCode
    sess_id = request.data.get('ocrSessionId')
    if not sess_id:
        raise BizError('缺少 ocrSessionId', code=ErrorCode.PARAM_ERROR)
    data = ocr_services.resolve(sess_id, request.user_id, request.data.get('chosenIndex'))
    return ok(data)
