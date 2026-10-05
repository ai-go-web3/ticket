"""屏2 · 上浮加价规则 CRUD + 计价预览。

写操作（建/改/启停/删）成功后统一清规则缓存 markup.clear_rules_cache()，即时生效，
并落一条 AdminOpLog 审计。列表/预览为只读。
所有接口须显式覆盖 @authentication_classes，否则被 C 端默认鉴权拦成 401（见 §3）。
"""
import logging

from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.models import AdminOpLog
from apps.adminapi.permissions import IsAdmin
from apps.adminapi.serializers import MarkupRuleSerializer
from apps.catalog import markup
from apps.catalog.models import MarkupRule, Schedule
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


def _get_rule(rule_id):
    rule = MarkupRule.objects.filter(id=rule_id, deleted=0).first()
    if not rule:
        raise BizError('规则不存在', code=40400)
    return rule


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_list(request):
    """规则列表（未软删，按优先级升序）。"""
    qs = MarkupRule.objects.filter(deleted=0).order_by('priority', 'id')
    if request.query_params.get('active_only') == '1':
        qs = qs.filter(is_active=1)
    return ok({'items': MarkupRuleSerializer(qs, many=True).data})


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_create(request):
    """新建规则。"""
    ser = MarkupRuleSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    rule = ser.save()
    markup.clear_rules_cache()
    _log(request, 'rule.create', rule.id, MarkupRuleSerializer(rule).data)
    return ok(MarkupRuleSerializer(rule).data, msg='已创建')


@api_view(['PUT'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_update(request, rule_id):
    """全量/部分更新规则（partial 由是否传 dict 决定，这里走部分更新更稳）。"""
    rule = _get_rule(rule_id)
    ser = MarkupRuleSerializer(rule, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    markup.clear_rules_cache()
    _log(request, 'rule.update', rule.id, ser.data)
    return ok(ser.data, msg='已更新')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_toggle(request, rule_id):
    """启用/停用切换（或显式传 is_active）。"""
    rule = _get_rule(rule_id)
    if 'is_active' in request.data:
        rule.is_active = 1 if int(request.data['is_active'] or 0) == 1 else 0
    else:
        rule.is_active = 0 if rule.is_active == 1 else 1
    rule.save(update_fields=['is_active', 'updated_at'])
    markup.clear_rules_cache()
    _log(request, 'rule.toggle', rule.id, {'is_active': rule.is_active})
    return ok(MarkupRuleSerializer(rule).data, msg='已启用' if rule.is_active else '已停用')


@api_view(['DELETE'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_delete(request, rule_id):
    """软删规则（deleted=1），保留审计可追溯。"""
    rule = _get_rule(rule_id)
    rule.deleted = 1
    rule.is_active = 0
    rule.save(update_fields=['deleted', 'is_active', 'updated_at'])
    markup.clear_rules_cache()
    _log(request, 'rule.delete', rule_id, {'name': rule.name})
    return ok(None, msg='已删除')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def rules_preview(request):
    """计价预览：给定场次 + 原始成本(分)，返回命中规则与上浮后售价。

    body: { schedule_id, cost_fen }（cost_fen 可缺省，仅看命中规则）。
    用于运营验证「这个场次当前会用哪档加价、成本 X 元卖多少钱」，不落库。
    """
    schedule_id = request.data.get('schedule_id')
    cost_fen = request.data.get('cost_fen')
    try:
        cost_fen = int(cost_fen) if cost_fen not in (None, '') else None
    except (TypeError, ValueError):
        raise BizError('cost_fen 需为整数(分)', code=40001)

    schedule = Schedule.objects.filter(id=schedule_id, deleted=0).first() if schedule_id else None
    if schedule_id and not schedule:
        raise BizError('场次不存在', code=40400)

    if schedule:
        mode, rate, flat_fen = markup.resolve_for_schedule(schedule)
    else:
        mode, rate, flat_fen = markup.resolve_markup(
            movie_id=request.data.get('movie_id'),
            brand=request.data.get('brand'),
            city_code=request.data.get('city_code'),
            hall_type=request.data.get('hall_type'),
        )

    data = {
        'mode': mode, 'rate': rate, 'flat_fen': flat_fen,
        'fallback': (mode, rate, flat_fen) == markup._fallback(),
        'schedule_id': schedule.id if schedule else None,
    }
    if cost_fen is not None:
        data['cost_fen'] = cost_fen
        data['sale_fen'] = markup.uplift_fen(cost_fen, mode, rate, flat_fen)
    return ok(data)
