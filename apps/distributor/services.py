"""分销服务：归因绑定 / 计佣 / 钱包 / 提现。"""
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_invite_code, gen_withdraw_no
from apps.distributor.models import (
    Distributor, CommissionRule, CommissionRecord, Wallet, WalletTxn, Withdrawal,
)
from apps.order.models import TicketOrder

logger = logging.getLogger('app')


def ensure_distributor(user_id):
    """确保用户有分销关系记录（自动建 + 生成推广码）。"""
    dist, created = Distributor.objects.get_or_create(
        user_id=user_id,
        defaults={
            'invite_code': gen_invite_code(),
            'level_path': f'/{user_id}/',
            'bind_at': timezone.now(),
            'source': 1,
        },
    )
    return dist


def bind_inviter(user_id, invite_code):
    """归因绑定：访客带 inviteCode 进入，绑定上级（层级≤2，防自绑/成环）。"""
    if not invite_code:
        return
    try:
        inviter = Distributor.objects.get(invite_code=invite_code, status=1)
    except Distributor.DoesNotExist:
        return

    if inviter.user_id == user_id:
        return  # 防自绑

    # 已绑定则不默认改绑
    existing = Distributor.objects.filter(user_id=user_id).first()
    if existing and existing.parent_id:
        return

    parent_id = inviter.user_id
    grand_id = inviter.parent_id  # 间接上级

    dist, _ = Distributor.objects.get_or_create(
        user_id=user_id,
        defaults={
            'invite_code': gen_invite_code(),
            'parent_id': parent_id,
            'grand_id': grand_id,
            'level_path': f'{inviter.level_path}{user_id}/',
            'bind_at': timezone.now(),
            'source': 1,
        },
    )
    if not dist.parent_id:
        dist.parent_id = parent_id
        dist.grand_id = grand_id
        dist.level_path = f'{inviter.level_path}{user_id}/'
        dist.save()
    return dist


def calc_commission(order):
    """计佣（确认收货后调用）：按差价模式，层级≤2。"""
    if not order.settle_amount:
        return []

    dist = Distributor.objects.filter(user_id=order.promoter_id).first() if order.promoter_id else None
    records = []
    base = order.pay_amount
    settle = order.settle_amount

    # 直属（promoter）
    if order.promoter_id:
        diff = max(base - settle, 0)
        records.append(_build_record(order, order.promoter_id, 1, diff))
        # 间接（grand）
        if dist and dist.grand_id:
            records.append(_build_record(order, dist.grand_id, 2, diff // 2))

    return records


def _build_record(order, promoter_id, level, amount):
    import uuid
    return CommissionRecord(
        rec_no=f"CM{uuid.uuid4().hex[:16].upper()}",
        order_id=order.id,
        buyer_id=order.user_id,
        promoter_id=promoter_id,
        level=level,
        base_amount=order.pay_amount,
        settle_amount=order.settle_amount or 0,
        commission_amount=amount,
        type=CommissionRecord.TYPE_INCOME,
        status=CommissionRecord.STATUS_PENDING,
    )


def settle_commission(order):
    """确认收货时计佣（在途）。"""
    records = calc_commission(order)
    if records:
        CommissionRecord.objects.bulk_create(records, ignore_conflicts=True)
    return records


def settle_wallet(promoter_id, amount):
    """佣金结算入钱包（T+N 后调用），CAS 更新。"""
    wallet, _ = Wallet.objects.get_or_create(user_id=promoter_id)
    updated = Wallet.objects.filter(
        user_id=promoter_id, version=wallet.version,
    ).update(
        balance=F('balance') + amount,
        total_income=F('total_income') + amount,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('钱包更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_SETTLE,
        amount=amount, balance_after=wallet.balance,
    )
    return wallet


def apply_withdraw(user_id, amount, channel=1):
    """申请提现（先冻结后打款）。"""
    from apps.common.utils import yuan_to_fen

    wallet = Wallet.objects.get(user_id=user_id)
    if wallet.balance < amount:
        raise BizError('余额不足', code=ErrorCode.WITHDRAW_INSUFFICIENT)

    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        balance=F('balance') - amount,
        frozen=F('frozen') + amount,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('钱包更新冲突，请重试')

    wallet.refresh_from_db()
    wd = Withdrawal.objects.create(
        wd_no=gen_withdraw_no(),
        user_id=user_id,
        amount=amount,
        channel=channel,
        status=Withdrawal.STATUS_WAIT,
    )
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_FREEZE,
        amount=-amount, ref_no=wd.wd_no, balance_after=wallet.balance,
    )
    return wd
