"""积分商城 · B 端后台（Story D）。

商品 CRUD + 上下架 + 调库存 + 兑换流水 + 运营看板。
全部 @authentication_classes(AdminJWT) + @permission_classes(IsAdmin) 显式覆盖，
否则被 C 端默认鉴权拦成 401（见 views_recommend 注释）。写操作落 AdminOpLog 审计。

删除策略：软删（deleted=1 且 status=下架），已有兑换记录的商品禁止物理删（保留对账）。
"""
import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.models import AdminOpLog
from apps.adminapi.permissions import IsAdmin
from apps.adminapi.serializers import MallItemSerializer
from apps.common.response import ok, BizError, ErrorCode
from apps.points.models import MallItem, MallRedemption, Voucher

logger = logging.getLogger('app')

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)


def _log(request, action, target, detail=None):
    try:
        admin = getattr(request, 'admin', None)
        AdminOpLog.objects.create(
            admin_id=getattr(admin, 'id', 0), action=action,
            target=str(target), detail=detail,
            ip=request.META.get('REMOTE_ADDR'),
        )
    except Exception:  # noqa: BLE001
        logger.exception('admin op log failed: %s', action)


def _get_item(item_id):
    item = MallItem.objects.filter(id=item_id, deleted=0).first()
    if not item:
        raise BizError('商品不存在', code=ErrorCode.MALL_NOT_FOUND)
    return item


# ===== 商品 CRUD =====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def items_list(request):
    """商品列表（未软删）。query: status(1/0), category, on_sale_only=1, kw(名称模糊)."""
    qs = MallItem.objects.filter(deleted=0)
    if request.query_params.get('status') in ('0', '1'):
        qs = qs.filter(status=int(request.query_params['status']))
    if request.query_params.get('category'):
        qs = qs.filter(category=int(request.query_params['category']))
    if request.query_params.get('on_sale_only') == '1':
        qs = qs.filter(status=MallItem.STATUS_ON)
    kw = request.query_params.get('kw')
    if kw:
        qs = qs.filter(name__icontains=kw)
    return ok({'items': MallItemSerializer(qs, many=True).data})


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def items_create(request):
    """新建商品。未给 stock 时用 stock_total 兜底（初始满库存）。默认下架，需再 publish。"""
    data = dict(request.data or {})
    if data.get('stock') in (None, '') and data.get('stock_total') not in (None, ''):
        data['stock'] = data['stock_total']
    data.setdefault('status', MallItem.STATUS_OFF)
    ser = MallItemSerializer(data=data)
    ser.is_valid(raise_exception=True)
    item = ser.save()
    _log(request, 'mall.item.create', item.id, ser.data)
    return ok(MallItemSerializer(item).data, msg='已创建')


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_detail(request, item_id):
    return ok(MallItemSerializer(_get_item(item_id)).data)


@api_view(['PUT', 'PATCH'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_update(request, item_id):
    """更新商品（部分更新）。改 stock_total 不自动联动 stock（调库存走专用接口）。"""
    item = _get_item(item_id)
    ser = MallItemSerializer(item, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    item = ser.save()
    _log(request, 'mall.item.update', item.id, ser.data)
    return ok(ser.data, msg='已更新')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_publish(request, item_id):
    """上架。校验有剩余库存才可上架。"""
    item = _get_item(item_id)
    if item.stock <= 0:
        raise BizError('库存为 0，请先调库存再上架', code=ErrorCode.MALL_STOCK_EMPTY)
    item.status = MallItem.STATUS_ON
    item.save(update_fields=['status', 'updated_at'])
    _log(request, 'mall.item.publish', item.id)
    return ok(MallItemSerializer(item).data, msg='已上架')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_unpublish(request, item_id):
    """下架（C 端立即不可见 / 兑换报 ITEM_OFFLINE）。"""
    item = _get_item(item_id)
    item.status = MallItem.STATUS_OFF
    item.save(update_fields=['status', 'updated_at'])
    _log(request, 'mall.item.unpublish', item.id)
    return ok(MallItemSerializer(item).data, msg='已下架')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_adjust_stock(request, item_id):
    """调整库存。body: {delta:int}（正=补货 负=回收），或 {stock:int}（直接设定剩余）。
    同步抬升 stock_total 保证 sold 口径正确（补货时）。"""
    item = _get_item(item_id)
    data = request.data or {}
    if 'stock' in data and data['stock'] is not None:
        new_stock = int(data['stock'])
    elif 'delta' in data and data['delta'] is not None:
        new_stock = item.stock + int(data['delta'])
    else:
        raise BizError('需提供 stock 或 delta', code=ErrorCode.PARAM_ERROR)
    if new_stock < 0:
        raise BizError('剩余库存不能为负', code=ErrorCode.PARAM_ERROR)
    with transaction.atomic():
        item = MallItem.objects.select_for_update().get(id=item.id)
        diff = new_stock - item.stock
        item.stock = new_stock
        # 补货时把总库存同步抬高，保证 sold=total-stock 反映真实累计可售
        if diff > 0:
            item.stock_total = item.stock_total + diff
        item.save(update_fields=['stock', 'stock_total', 'updated_at'])
    _log(request, 'mall.item.adjust_stock', item.id, {'new_stock': new_stock})
    return ok(MallItemSerializer(item).data, msg='库存已更新')


@api_view(['DELETE'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def item_delete(request, item_id):
    """软删（下架 + deleted=1）。有兑换记录也允许（历史记录保留在对账，商品仅从前台移除）。"""
    item = _get_item(item_id)
    item.status = MallItem.STATUS_OFF
    item.deleted = 1
    item.save(update_fields=['status', 'deleted', 'updated_at'])
    _log(request, 'mall.item.delete', item_id)
    return ok(None, msg='已删除')


# ===== 兑换流水 =====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def redemptions(request):
    """兑换流水列表。query: itemId, userId, category, days(近 N 天, 默认 7), page/limit."""
    qs = MallRedemption.objects.all()
    if request.query_params.get('itemId'):
        qs = qs.filter(item_id=int(request.query_params['itemId']))
    if request.query_params.get('userId'):
        qs = qs.filter(user_id=int(request.query_params['userId']))
    if request.query_params.get('category'):
        qs = qs.filter(category=int(request.query_params['category']))
    days = int(request.query_params.get('days') or 7)
    if days > 0:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    limit = min(int(request.query_params.get('limit') or 50), 200)
    rows = [{
        'redemptionNo': r.redemption_no, 'userId': r.user_id, 'itemId': r.item_id,
        'itemName': r.item_name, 'category': r.category,
        'categoryText': MallItem.CAT_TEXT.get(r.category, ''),
        'pointsPrice': r.points_price, 'quantity': r.quantity, 'createdAt': r.created_at,
    } for r in qs.order_by('-created_at')[:limit]]
    return ok({'items': rows})


# ===== 运营看板 =====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def dashboard(request):
    """兑换看板：今日/累计兑换、消耗积分、库存告警、品类分布、券核销率。"""
    now = timezone.now()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days = int(request.query_params.get('days') or 30)

    today_qs = MallRedemption.objects.filter(created_at__gte=today)
    total_qs = MallRedemption.objects.filter(created_at__gte=now - timedelta(days=days))

    today_cnt = today_qs.count()
    today_points = today_qs.aggregate(s=Sum('points_price'))['s'] or 0
    period_cnt = total_qs.count()
    period_points = total_qs.aggregate(s=Sum('points_price'))['s'] or 0

    # 品类分布（近 N 天）
    cat_dist = list(total_qs.values('category').annotate(c=Count('id')).order_by('category'))
    cat_rows = [{'category': x['category'],
                 'categoryText': MallItem.CAT_TEXT.get(x['category'], ''),
                 'count': x['c']} for x in cat_dist]

    # 库存告警：上架且剩余占比 <=20% 或已兑完
    on_items = MallItem.objects.filter(status=MallItem.STATUS_ON, deleted=0)
    alerts = []
    for it in on_items:
        if it.stock_status in ('low', 'out'):
            alerts.append({'id': it.id, 'name': it.name, 'stock': it.stock,
                           'stockTotal': it.stock_total, 'stockStatus': it.stock_status})

    # 券核销率（近 N 天派生的券）
    vstart = now - timedelta(days=days)
    v_total = Voucher.objects.filter(created_at__gte=vstart).count()
    v_used = Voucher.objects.filter(created_at__gte=vstart, status=Voucher.STATUS_USED).count()

    return ok({
        'today': {'count': today_cnt, 'points': today_points},
        'period': {'days': days, 'count': period_cnt, 'points': period_points},
        'categoryDist': cat_rows,
        'stockAlerts': alerts,
        'voucher': {'total': v_total, 'used': v_used,
                    'usedRate': round(v_used / v_total, 4) if v_total else 0},
    })
