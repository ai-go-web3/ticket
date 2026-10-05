"""屏1 · 数据看板视图。"""
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi import services
from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.permissions import IsAdmin
from apps.common.response import ok

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)


def _range(request):
    r = request.query_params.get('range', '7d')
    return r if r in ('today', '7d', '30d') else '7d'


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def overview(request):
    """6 张核心 KPI。"""
    return ok(services.overview(_range(request)))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def trend(request):
    """GMV/毛利按日趋势（复用 daily_report）。range: 7d/30d。"""
    days = 30 if request.query_params.get('range') == '30d' else 7
    return ok(services.trend(days))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def status_count(request):
    """各状态订单条数（9 态，仅条数，不做泳道）。"""
    return ok(services.status_count(_range(request)))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def top(request):
    """Top 榜单：dim=movie|brand。"""
    dim = request.query_params.get('dim', 'movie')
    if dim == 'brand':
        return ok(services.top_brands(_range(request)))
    return ok(services.top_movies(_range(request)))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def alerts(request):
    """需关注：未结算 / 倒挂 单号。"""
    return ok(services.alerts_brief())
