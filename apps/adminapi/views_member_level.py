"""会员等级配置 · B 端后台。

等级 CRUD：成长值 → 等级 → 消费返利率。取代旧 settings.POINTS['TIERS'] 硬编码。
全部 @authentication_classes(AdminJWT) + @permission_classes(IsAdmin) 显式覆盖，
否则被 C 端默认鉴权拦成 401（同 views_points_mall）。写操作落 AdminOpLog 审计。

删除策略：物理删（无外键引用），但保护"至少保留一档启用"，且不允许删掉
当前有用户挂靠的等级（Wallet.tier 里存在该 level 的账户时禁止删）。
"""
import logging

from django.db import IntegrityError, transaction
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.models import AdminOpLog
from apps.adminapi.permissions import IsAdmin
from apps.adminapi.serializers import MemberLevelSerializer
from apps.common.response import ok, BizError, ErrorCode
from apps.distributor.models import MemberLevel, Wallet

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


def _get_level(level_id):
    row = MemberLevel.objects.filter(id=level_id).first()
    if not row:
        raise BizError('等级不存在', code=ErrorCode.MEMBER_LEVEL_NOT_FOUND)
    return row


def _check_growth_order(instance, level, growth_min):
    """门槛需按 level 递增：低 level 的 growth_min 必须 ≤ 当前；高 level 的 growth_min 必须 ≥ 当前。"""
    qs = MemberLevel.objects.exclude(id=instance.id if instance else None)
    lower = qs.filter(level__lt=level).order_by('-level').first()
    higher = qs.filter(level__gt=level).order_by('level').first()
    if lower and int(growth_min) < int(lower.growth_min):
        raise BizError(
            f'成长值门槛需 ≥ 低等级 LV{lower.level} 的 {lower.growth_min}',
            code=ErrorCode.MEMBER_LEVEL_GROWTH_ORDER,
        )
    if higher and int(growth_min) > int(higher.growth_min):
        raise BizError(
            f'成长值门槛需 ≤ 高等级 LV{higher.level} 的 {higher.growth_min}',
            code=ErrorCode.MEMBER_LEVEL_GROWTH_ORDER,
        )


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def levels_list(request):
    """等级列表（按 level 升序）。query: active_only=1 只看启用。"""
    qs = MemberLevel.objects.all()
    if request.query_params.get('active_only') == '1':
        qs = qs.filter(is_active=1)
    return ok({'items': MemberLevelSerializer(qs, many=True).data})


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def levels_create(request):
    """新建等级。level 唯一；growth_min 按 level 递增。"""
    ser = MemberLevelSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data
    _check_growth_order(None, int(data['level']), int(data['growth_min']))
    try:
        with transaction.atomic():
            row = MemberLevel.objects.create(**data)
    except IntegrityError:
        raise BizError('等级序号已存在', code=ErrorCode.MEMBER_LEVEL_DUP)
    _log(request, 'member_level.create', row.id, data)
    return ok(MemberLevelSerializer(row).data, msg='已创建')


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def level_detail(request, level_id):
    return ok(MemberLevelSerializer(_get_level(level_id)).data)


@api_view(['PUT', 'PATCH'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def level_update(request, level_id):
    """更新等级（部分更新）。改门槛/序号时重新校验递增。"""
    row = _get_level(level_id)
    ser = MemberLevelSerializer(row, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data
    new_level = int(data.get('level', row.level))
    new_growth = int(data.get('growth_min', row.growth_min))
    _check_growth_order(row, new_level, new_growth)
    # 若把当前唯一启用档停用 → 拒绝
    if 'is_active' in data and int(data['is_active']) == 0 and row.is_active == 1:
        if MemberLevel.objects.filter(is_active=1).exclude(id=row.id).count() == 0:
            raise BizError('至少保留一档启用', code=ErrorCode.MEMBER_LEVEL_LAST_ACTIVE)
    try:
        with transaction.atomic():
            for k, v in data.items():
                setattr(row, k, v)
            row.save()
    except IntegrityError:
        raise BizError('等级序号已存在', code=ErrorCode.MEMBER_LEVEL_DUP)
    _log(request, 'member_level.update', row.id, data)
    return ok(MemberLevelSerializer(row).data, msg='已更新')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def level_toggle(request, level_id):
    """启用/停用切换。body 可选 {is_active: 0|1}，不传则取反。"""
    row = _get_level(level_id)
    want = request.data.get('is_active')
    new_val = int(want) if want is not None else (0 if row.is_active == 1 else 1)
    if new_val not in (0, 1):
        raise BizError('is_active 只能为 0 或 1', code=ErrorCode.PARAM_ERROR)
    if new_val == 0 and row.is_active == 1:
        if MemberLevel.objects.filter(is_active=1).exclude(id=row.id).count() == 0:
            raise BizError('至少保留一档启用', code=ErrorCode.MEMBER_LEVEL_LAST_ACTIVE)
    row.is_active = new_val
    row.save(update_fields=['is_active', 'updated_at'])
    _log(request, 'member_level.toggle', row.id, {'is_active': new_val})
    return ok(MemberLevelSerializer(row).data, msg='已启用' if new_val else '已停用')


@api_view(['DELETE'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def level_delete(request, level_id):
    """物理删。禁止：唯一启用档 / 有用户挂靠的等级。"""
    row = _get_level(level_id)
    if row.is_active == 1 and MemberLevel.objects.filter(is_active=1).exclude(id=row.id).count() == 0:
        raise BizError('至少保留一档启用', code=ErrorCode.MEMBER_LEVEL_LAST_ACTIVE)
    bound = Wallet.objects.filter(tier=row.level).count()
    if bound > 0:
        raise BizError(
            f'该等级下有 {bound} 个用户挂靠，请先停用或迁移用户',
            code=ErrorCode.MEMBER_LEVEL_LAST_ACTIVE,
        )
    row.delete()
    _log(request, 'member_level.delete', level_id)
    return ok(None, msg='已删除')
