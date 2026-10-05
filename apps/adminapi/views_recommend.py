"""屏3 · 首页装修（本月推荐位）后台 CRUD。

推荐位 = 影片 / 横幅 混合轮播；另有单例「栏目设置」（标题 / 最多展示 / 空位回退）。
写操作落 AdminOpLog 审计；软删保留可追溯；不引入缓存（读取路径直接查库，量级小）。

preview 复用 apps/catalog/recommend.build_home_recommends()，与 C 端同一份装配，
运营在后台选城市即可所见即所得地核对线上效果。

所有接口显式覆盖 @authentication_classes，否则被 C 端默认鉴权拦成 401（见 views_rules 注释）。
"""
import logging

from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.models import AdminOpLog
from apps.adminapi.permissions import IsAdmin
from apps.adminapi.serializers import RecommendSectionSerializer, RecommendSlotSerializer
from apps.catalog.models import RecommendSection, RecommendSlot
from apps.catalog.recommend import build_home_recommends
from apps.common.response import ok, BizError

logger = logging.getLogger('app')

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)


def _log(request, action, target, detail=None):
    """写后台操作审计（best-effort，失败不影响主流程）。"""
    try:
        admin = getattr(request, 'admin', None)
        AdminOpLog.objects.create(
            admin_id=getattr(admin, 'id', 0),
            action=action,
            target=str(target),
            detail=detail,
            ip=request.META.get('REMOTE_ADDR'),
        )
    except Exception:  # noqa: BLE001 - 审计失败绝不影响业务
        logger.exception('admin op log failed: %s', action)


def _get_slot(slot_id):
    slot = RecommendSlot.objects.filter(id=slot_id, deleted=0).first()
    if not slot:
        raise BizError('推荐位不存在', code=40400)
    return slot


def _ser(obj=None, many=False, data=None):
    """统一带上 _movie_cache 上下文，避免列表逐条查影片。

    注意：data 为 None 时不能透传给 DRF（否则会被当成「已传入待校验数据」，
    读 .data 会报 must call .is_valid() 错）。
    """
    kwargs = {'many': many, 'context': {'_movie_cache': {}}}
    if data is not None:
        kwargs['data'] = data
    return RecommendSlotSerializer(obj, **kwargs)


# ===== 栏目设置（单例）=====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def section_get(request):
    """读取首页推荐栏目设置（无则自动创建默认单例）。"""
    return ok(RecommendSectionSerializer(RecommendSection.load()).data)


@api_view(['PUT', 'POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def section_save(request):
    """更新栏目设置（标题 / 最多展示条数 / 空位回退开关）。"""
    section = RecommendSection.load()
    ser = RecommendSectionSerializer(section, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    _log(request, 'recommend.section', section.id, ser.data)
    return ok(ser.data, msg='已保存')


# ===== 推荐位 CRUD =====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_list(request):
    """推荐位列表（未软删，按 sort、id 升序）。"""
    qs = RecommendSlot.objects.filter(deleted=0).order_by('sort', 'id')
    if request.query_params.get('enabled_only') == '1':
        qs = qs.filter(enabled=1)
    return ok({'items': _ser(qs, many=True).data})


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_create(request):
    """新建推荐位。未显式给 sort 时追加到末尾（当前最大 sort+1）。"""
    data = dict(request.data)
    if data.get('sort') in (None, ''):
        last = RecommendSlot.objects.filter(deleted=0).order_by('-sort').first()
        data['sort'] = (last.sort + 1) if last else 0
    ser = _ser(data=data)
    ser.is_valid(raise_exception=True)
    slot = ser.save()
    _log(request, 'recommend.slot.create', slot.id, ser.data)
    return ok(_ser(slot).data, msg='已创建')


@api_view(['PUT'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_update(request, slot_id):
    """更新推荐位（部分更新：只覆盖请求体里出现的字段，其余保持原值）。"""
    slot = _get_slot(slot_id)
    ser = _ser(slot, data=request.data)
    ser.is_valid(raise_exception=True)
    ser.save()
    _log(request, 'recommend.slot.update', slot.id, ser.data)
    return ok(ser.data, msg='已更新')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_toggle(request, slot_id):
    """启用/停用切换（或显式传 enabled）。"""
    slot = _get_slot(slot_id)
    if 'enabled' in request.data:
        slot.enabled = 1 if int(request.data['enabled'] or 0) == 1 else 0
    else:
        slot.enabled = 0 if slot.enabled == 1 else 1
    slot.save(update_fields=['enabled', 'updated_at'])
    _log(request, 'recommend.slot.toggle', slot.id, {'enabled': slot.enabled})
    return ok(_ser(slot).data, msg='已启用' if slot.enabled else '已停用')


@api_view(['DELETE'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_delete(request, slot_id):
    """软删推荐位（deleted=1，enabled=0）。"""
    slot = _get_slot(slot_id)
    slot.deleted = 1
    slot.enabled = 0
    slot.save(update_fields=['deleted', 'enabled', 'updated_at'])
    _log(request, 'recommend.slot.delete', slot_id, {'slot_type': slot.slot_type})
    return ok(None, msg='已删除')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_reorder(request):
    """批量重排：body {ids:[slot_id,...]}，按数组顺序回写 sort（0..n-1）。

    仅更新传入且未软删的位；未传的位保持原 sort（可能被挤到后面）。
    """
    ids = request.data.get('ids') or []
    if not isinstance(ids, list) or not ids:
        raise BizError('ids 需为数组', code=40001)
    updated = 0
    for pos, sid in enumerate(ids):
        slot = RecommendSlot.objects.filter(id=sid, deleted=0).first()
        if not slot:
            continue
        if slot.sort != pos:
            slot.sort = pos
            slot.save(update_fields=['sort', 'updated_at'])
            updated += 1
    _log(request, 'recommend.slot.reorder', 'batch', {'count': updated})
    return ok({'updated': updated}, msg='已排序')


# ===== 预览（复用 C 端装配）=====
@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def slots_preview(request):
    """按城市预览线上实际会返回的推荐位（含空位回退逻辑）。query: cityCode。"""
    city_code = request.query_params.get('cityCode') or request.query_params.get('city_code')
    return ok(build_home_recommends(city_code))
