"""distributor 视图。"""
import logging

from rest_framework.decorators import api_view
from rest_framework import serializers

from django.db.models import Sum

from apps.common.response import ok, BizError
from apps.distributor import services
from apps.distributor.models import Distributor, CommissionRecord, Wallet, WalletTxn, Withdrawal

logger = logging.getLogger('app')


class BindSerializer(serializers.Serializer):
    inviteCode = serializers.CharField(required=False, allow_blank=True)


class WithdrawSerializer(serializers.Serializer):
    amount = serializers.IntegerField(min_value=1)  # 单位：分
    channel = serializers.IntegerField(default=1)


@api_view(['POST'])
def bind(request):
    """归因绑定。"""
    ser = BindSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    services.bind_inviter(request.user_id, ser.validated_data.get('inviteCode'))
    return ok(None)


@api_view(['GET'])
def summary(request):
    """分销概览（推广码/邀请数/在途佣金）。"""
    dist = services.ensure_distributor(request.user_id)
    pending = sum(
        CommissionRecord.objects.filter(
            promoter_id=request.user_id, status=CommissionRecord.STATUS_PENDING,
        ).values_list('commission_amount', flat=True),
    )
    direct = Distributor.objects.filter(parent_id=request.user_id).count()
    return ok({
        'inviteCode': dist.invite_code,
        'invited': direct,
        'pendingCommission': pending,
    })


@api_view(['GET'])
def team(request):
    """我的团队。"""
    level = request.query_params.get('level', '1')
    qs = Distributor.objects.filter(parent_id=request.user_id)
    return ok([{'userId': d.user_id, 'bindAt': d.bind_at} for d in qs])


@api_view(['GET'])
def commissions(request):
    """佣金流水。"""
    qs = CommissionRecord.objects.filter(promoter_id=request.user_id).order_by('-created_at')
    return ok([{
        'recNo': r.rec_no, 'orderId': r.order_id, 'level': r.level,
        'amount': r.commission_amount, 'type': r.type, 'status': r.status,
        'createdAt': r.created_at,
    } for r in qs])


@api_view(['GET'])
def wallet(request):
    """钱包余额（金额单位：分）。"""
    from datetime import date
    w, _ = Wallet.objects.get_or_create(user_id=request.user_id)
    today_income = CommissionRecord.objects.filter(
        promoter_id=request.user_id,
        type=CommissionRecord.TYPE_INCOME,
        status=CommissionRecord.STATUS_SETTLED,
        settle_at__date=date.today(),
    ).aggregate(total=Sum('commission_amount'))['total'] or 0
    return ok({
        'balance': w.balance, 'frozen': w.frozen,
        'totalIncome': w.total_income, 'totalWithdraw': w.total_withdraw,
        'todayIncome': today_income,
    })


@api_view(['POST'])
def withdraw(request):
    """申请提现。"""
    ser = WithdrawSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    wd = services.apply_withdraw(
        request.user_id, ser.validated_data['amount'], ser.validated_data['channel'],
    )
    return ok({'wdNo': wd.wd_no, 'status': wd.status})
