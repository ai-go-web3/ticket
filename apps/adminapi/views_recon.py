"""屏4 · 对账与盈利（复用 daily_report，口径别重写）。"""
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi import services
from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.permissions import IsAdmin
from apps.common.response import ok

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def finance_daily(request):
    """财务日报（10 列口径，对齐 docs/财务口径与对账.md）。

    query: days(默认7, 1-90) 或 date(YYYY-MM-DD)。直接包 order.reporting.daily_report。
    """
    try:
        days = int(request.query_params.get('days', 7))
    except (TypeError, ValueError):
        days = 7
    days = max(1, min(days, 90))
    return ok(services.trend(days))   # trend 即 daily_report(days).days + alerts


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def finance_summary(request):
    """盈利概览 KPI + 趋势。range: 7d/30d。"""
    r = request.query_params.get('range', '7d')
    return ok({
        'kpi': services.overview(r if r in ('today', '7d', '30d') else '7d'),
        'trend': services.trend(30 if r == '30d' else 7),
    })
