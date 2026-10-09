"""distributor/积分 视图。

现行 = 消费积分体系：/distributor/points/* 提供积分账户/流水/试算；
wallet / credit/quote 为兼容别名（返回积分口径）。
分销遗留接口（bind/summary/team/commissions/withdraw）在 DISTRIBUTOR_ENABLED=关时
一律停用（空数据 / 停用提示），保留路由不报错。
"""
import logging

from django.db.models import Sum
from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok, BizError, ErrorCode
from apps.distributor import services
from apps.distributor.models import Distributor, CommissionRecord, WalletTxn

logger = logging.getLogger('app')


def _distributor_on():
    from django.conf import settings as dj_settings
    return bool(getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False))


class BindSerializer(serializers.Serializer):
    inviteCode = serializers.CharField(required=False, allow_blank=True)


class WithdrawSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)
    channel = serializers.IntegerField(default=1)


@api_view(['POST'])
def bind(request):
    """归因绑定（分销停用态：不写入，直接返回）。"""
    ser = BindSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    services.bind_inviter(request.user_id, ser.validated_data.get('inviteCode'))
    return ok(None)


@api_view(['GET'])
def summary(request):
    """分销概览（停用态返回空）。"""
    if not _distributor_on():
        return ok({'inviteCode': '', 'invited': 0, 'pendingCommission': 0, 'disabled': True})
    dist = services.ensure_distributor(request.user_id)
    pending = sum(
        CommissionRecord.objects.filter(
            promoter_id=request.user_id, status=CommissionRecord.STATUS_PENDING,
        ).values_list('commission_amount', flat=True),
    )
    direct = Distributor.objects.filter(parent_id=request.user_id).count()
    return ok({
        'inviteCode': dist.invite_code if dist else '',
        'invited': direct,
        'pendingCommission': pending,
    })


@api_view(['GET'])
def team(request):
    """我的团队（停用态返回空）。"""
    if not _distributor_on():
        return ok([])
    qs = Distributor.objects.filter(parent_id=request.user_id)
    return ok([{'userId': d.user_id, 'bindAt': d.bind_at} for d in qs])


@api_view(['GET'])
def commissions(request):
    """佣金流水（遗留，停用态返回空）。"""
    if not _distributor_on():
        return ok([])
    qs = CommissionRecord.objects.filter(promoter_id=request.user_id).order_by('-created_at')
    return ok([{
        'recNo': r.rec_no, 'orderId': r.order_id, 'level': r.level,
        'amount': r.commission_amount, 'type': r.type, 'status': r.status,
        'createdAt': r.created_at,
    } for r in qs])


# ---------------------------------------------------------------------------
# 积分（现行）
# ---------------------------------------------------------------------------
@api_view(['GET'])
def points_account(request):
    """积分账户概览：余额/冻结/成长值/等级 + 抵扣规则。"""
    return ok(services.get_points_account(request.user_id))


@api_view(['GET'])
def points_transactions(request):
    """积分流水（明细展示）。"""
    try:
        limit = int(request.query_params.get('limit', 50) or 50)
    except (TypeError, ValueError):
        limit = 50
    return ok(services.list_transactions(request.user_id, limit=max(1, min(limit, 200))))


@api_view(['GET'])
def points_quote(request):
    """结算页试算：本单可用积分抵扣额（不落库）。amount 单位：分。"""
    amount = int(request.query_params.get('amount', 0) or 0)
    activity_ref = request.query_params.get('activityRef') or None
    return ok(services.quote_deduct(amount, request.user_id, activity_ref))


@api_view(['GET'])
def wallet(request):
    """积分账户（兼容别名，键沿用旧 wallet）：balance/frozen 单位=积分。"""
    from datetime import date
    data = services.get_points_account(request.user_id)
    w = services.get_wallet(request.user_id)
    today_income = WalletTxn.objects.filter(
        wallet_id=w.id, biz_type=WalletTxn.BIZ_SETTLE,
        created_at__date=date.today(),
    ).aggregate(total=Sum('amount'))['total'] or 0
    return ok({
        'balance': data['balance'], 'frozen': data['frozen'],
        'availableCredit': data['balance'],
        'growth': data['growth'], 'tier': data['tier'], 'tierName': data['tierName'],
        'totalIncome': data['totalIncome'], 'totalWithdraw': data['totalWithdraw'],
        'todayIncome': int(today_income),
        'fenPerPoint': data['fenPerPoint'],
        'deductRule': data['deductRule'],
    })


@api_view(['POST'])
def withdraw(request):
    """申请提现（积分体系已停用，保留接口仅返回停用提示）。"""
    raise BizError(
        '提现已停用，积分仅可抵扣电影票',
        code=ErrorCode.WITHDRAW_DISABLED,
    )


# 兼容旧路径名
deduct_quote = points_quote
