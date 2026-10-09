"""积分服务：账户/等级 / 消费返积分入账 / 抵扣资金流 / 遗留分销计佣（停用）。

现行方向 = 消费积分体系（合规：奖励只挂用户自身消费/行为，与拉人无关）：
- 获取：earn_consume(消费返积分)；后续可扩 签到/任务/生日/后台直发（scene 细分）。
- 抵扣生命周期：quote_deduct(试算) -> freeze_deduct(下单冻结)
  -> settle_deduct(支付成功核销) / unfreeze_deduct(支付失败解冻)；退款回冲
  deduct_reverse_on_refund；过期 expire_deduct。
- 单位：钱包 balance/frozen 与流水 amount 均为「积分」；订单金额/抵扣上限等为「分」，
  二者按 DeductRule.fen_per_point 换算（默认 1 积分 = 1 分，即 100 积分 = 1 元）。
- 分销那套（归因/计佣/提现）：DISTRIBUTOR_ENABLED 开关门控，默认关（停用保留、可回滚）。
"""
import logging
import uuid
from decimal import Decimal

from django.conf import settings as dj_settings
from django.db import transaction
from django.db.models import F, Q, Sum
from django.utils import timezone

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_invite_code
from apps.distributor.models import (
    Distributor, CommissionRecord, Wallet, WalletTxn, DeductRule,
)

logger = logging.getLogger('app')

# 全局兜底抵扣规则（DB 无启用规则时使用，避免空配置崩链路）
_DEFAULT_RULE = {
    'fen_per_point': Decimal('1.0000'),        # 1 积分抵扣 1 分
    'max_deduct_ratio': Decimal('0.5000'),     # 单笔最多抵 50%
    'min_self_pay': 100,                        # 最低自付 1 元
    'per_order_cap': 0,                         # 单笔抵扣上限(分)，0=不限
    'daily_deduct_cap': 0,                      # 每人每日抵扣上限(分)，0=不限
    'expire_days': 365,
    'allow_grand': 0,
    'stack_with_coupon': 0,
}

# 获取子类的展示文案
_SCENE_TEXT = {
    WalletTxn.SCENE_CONSUME: '消费返积分',
    WalletTxn.SCENE_CHECKIN: '每日签到',
    WalletTxn.SCENE_TASK: '任务奖励',
    WalletTxn.SCENE_BIRTHDAY: '生日/会员日',
    WalletTxn.SCENE_ADMIN: '平台奖励',
    WalletTxn.SCENE_EXCHANGE: '兑换',
}


def _points_enabled():
    return bool(dj_settings.POINTS.get('ENABLED', True))


def _fpp(rule):
    """兑换率：1 积分抵扣多少「分」。默认 1。"""
    try:
        v = float(rule.fen_per_point)
    except (TypeError, ValueError):
        v = 1.0
    return v if v > 0 else 1.0


# ---------------------------------------------------------------------------
# 账户 / 等级
# ---------------------------------------------------------------------------
def get_wallet(user_id):
    """获取或创建积分账户。"""
    wallet, _ = Wallet.objects.get_or_create(user_id=user_id)
    return wallet


def tier_by_growth(growth):
    """按成长值落等级（取 settings.POINTS['TIERS']，返回 (level, name, rate)。"""
    tiers = dj_settings.POINTS.get('TIERS') or []
    base_rate = float(dj_settings.POINTS.get('CONSUME_RATE', 0) or 0)
    hit = (0, '新影迷', base_rate)
    for t in sorted(tiers, key=lambda x: x.get('growth_min', 0)):
        if int(growth or 0) >= int(t.get('growth_min', 0)):
            rate = float(t.get('rate', 0) or 0) or base_rate
            hit = (int(t.get('level', 0)), t.get('name', ''), rate)
    return hit


def recalc_tier(wallet):
    """按当前成长值重算并回写等级；返回 (level, name)。"""
    level, name, _rate = tier_by_growth(wallet.growth)
    if wallet.tier != level:
        Wallet.objects.filter(id=wallet.id).update(tier=level)
        wallet.tier = level
    return level, name


def get_deduct_rule(activity_ref=None):
    """取生效的抵扣规则：优先活动级，其次全局；均无则返回内存兜底规则。"""
    now = timezone.now()
    qs = (
        DeductRule.objects.filter(is_active=1)
        .filter(Q(effective_from__isnull=True) | Q(effective_from__lte=now))
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=now))
    )
    if activity_ref:
        r = qs.filter(scope=DeductRule.SCOPE_ACTIVITY, scope_ref=str(activity_ref)).order_by('-id').first()
        if r:
            return r
    r = qs.filter(scope=DeductRule.SCOPE_GLOBAL).order_by('-id').first()
    if r:
        return r
    return DeductRule(name='default', scope=DeductRule.SCOPE_GLOBAL, **_DEFAULT_RULE)


def get_points_account(user_id):
    """积分账户概览（供积分中心页）：余额/冻结/成长值/等级 + 抵扣规则展示。"""
    w = get_wallet(user_id)
    rule = get_deduct_rule()
    level, name, rate = tier_by_growth(w.growth)
    if w.tier != level:
        w.tier = level
        Wallet.objects.filter(id=w.id).update(tier=level)
    today = timezone.now().date()
    return {
        'balance': w.balance, 'frozen': w.frozen,
        'growth': w.growth, 'tier': w.tier, 'tierName': name,
        'totalIncome': w.total_income, 'totalWithdraw': w.total_withdraw,
        'checkedToday': (w.last_earn_date == today),
        'streak': w.streak,
        'fenPerPoint': _fpp(rule),
        'consumeRate': rate,
        'deductRule': {
            'maxDeductRatio': float(rule.max_deduct_ratio),
            'minSelfPay': rule.min_self_pay,
            'expireDays': rule.expire_days,
            'fenPerPoint': _fpp(rule),
        },
    }


# ---------------------------------------------------------------------------
# 获取：消费返积分（E1）
# ---------------------------------------------------------------------------
def _today_earned(wallet_id):
    """当日已获取积分（BIZ_SETTLE 正额合计），用于日获取上限。"""
    agg = WalletTxn.objects.filter(
        wallet_id=wallet_id, biz_type=WalletTxn.BIZ_SETTLE,
        created_at__date=timezone.now().date(),
    ).aggregate(s=Sum('amount'))['s'] or 0
    return max(int(agg), 0)


@transaction.atomic
def earn(order, points, scene, ref_no=None, remark=None, allow_zero=False):
    """积分入账最小单元（原子 + CAS）：balance/growth/total_income 同增，重算等级，记流水。

    points<=0 默认跳过（不写 0 流水）；幂等由调用方（如 earn_consume 的 ref_no 去重）保证。
    """
    points = int(points or 0)
    wallet = get_wallet(order.user_id if hasattr(order, 'user_id') else order)
    if points <= 0 and not allow_zero:
        return wallet
    # 日获取上限 / 余额上限（风控钳制）
    cfg = dj_settings.POINTS
    daily_cap = int(cfg.get('DAILY_EARN_CAP', 0) or 0)
    if daily_cap:
        points = min(points, max(daily_cap - _today_earned(wallet.id), 0))
    bal_cap = int(cfg.get('BALANCE_CAP', 0) or 0)
    if bal_cap:
        points = min(points, max(bal_cap - wallet.balance, 0))
    if points <= 0 and not allow_zero:
        return wallet
    updated = Wallet.objects.filter(
        user_id=wallet.user_id, version=wallet.version,
    ).update(
        balance=F('balance') + points,
        growth=F('growth') + points,
        total_income=F('total_income') + points,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    recalc_tier(wallet)
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_SETTLE, scene=scene,
        amount=points, ref_no=ref_no, balance_after=wallet.balance,
        remark=remark or _SCENE_TEXT.get(scene, '积分入账'),
    )
    return wallet


@transaction.atomic
def earn_consume(order):
    """消费返积分：确认收货(DONE)后，按订单实付现金 × 等级返利率入账。

    - 只对「自身消费」返利，与是否邀请到人无关（合规根基）；
    - 封顶：不超毛利（pay_amount − settle_amount，settle 缺失则不设该上限）、单笔上限；
    - 幂等：同单已记 SCENE_CONSUME 入账则跳过（查询/回调双路径可能重复触发 DONE）。
    - 未确认收货前不入账，规避「下单返完就退票」白嫖；退款则回冲（见下）。
    """
    if not _points_enabled():
        return None
    wallet = get_wallet(order.user_id)
    if WalletTxn.objects.filter(
            wallet_id=wallet.id, biz_type=WalletTxn.BIZ_SETTLE,
            scene=WalletTxn.SCENE_CONSUME, ref_no=order.order_ext_no).exists():
        return wallet  # 幂等：本单已返过
    _level, _name, rate = tier_by_growth(wallet.growth)
    cfg = dj_settings.POINTS
    fpp = _fpp(get_deduct_rule())
    base_fen = int(order.pay_amount or 0)          # 按实付现金计（积分抵扣部分不再返利）
    points = int((base_fen * rate) // fpp) if rate > 0 else 0
    # 封顶毛利：返积分价值(分) ≤ 毛利(分) × MARGIN_FACTOR
    if cfg.get('CAP_BY_MARGIN', True) and order.settle_amount is not None:
        margin_fen = max(base_fen - int(order.settle_amount), 0)
        max_by_margin = int((margin_fen * float(cfg.get('MARGIN_FACTOR', 1.0) or 1.0)) // fpp)
        points = min(points, max_by_margin)
    per_cap = int(cfg.get('PER_ORDER_CAP', 0) or 0)
    if per_cap:
        points = min(points, per_cap)
    if points <= 0:
        return wallet
    return earn(order, points, WalletTxn.SCENE_CONSUME,
                ref_no=order.order_ext_no, remark='消费返积分')


def confirm_settle(order):
    """确认收货(DONE)统一激励入口：先做积分（现行方向）；分销计佣受开关门控（默认关）。"""
    if _points_enabled():
        try:
            earn_consume(order)
        except Exception as exc:  # noqa: BLE001 返积分失败不影响出票主流程，留痕人工补
            logger.error('消费返积分失败 order=%s err=%s', order.order_ext_no, exc)
    if getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False):
        settle_commission(order)


# ---------------------------------------------------------------------------
# 抵扣资金流（单位：积分；订单侧金额单位：分，按 fen_per_point 换算）
# ---------------------------------------------------------------------------
def _today_deduct_used_points(wallet_id):
    """当日已抵扣（含冻结中，BIZ_FREEZE 记负数）积分，用于日累计上限换算。"""
    agg = WalletTxn.objects.filter(
        wallet_id=wallet_id, biz_type=WalletTxn.BIZ_FREEZE,
        created_at__date=timezone.now().date(),
    ).aggregate(s=Sum('amount'))['s'] or 0
    return -int(agg)  # FREEZE 负数，取正（积分）


def quote_deduct(order_amount_fen, user_id, activity_ref=None):
    """结算页试算：本单可用积分抵扣额（不落库）。

    入参 order_amount_fen = 待抵扣的商品应付（分）。
    抵扣金额(分) = min(余额可抵分, 订单额×最高比例, 订单额−最低自付, 单笔上限, 当日剩余额度)
    再按兑换率折算为积分（向下取整，宁可少抵扣、多自付，保证不超余额/规则）。
    返回 {points(积分), valueFen(实际抵扣分), selfPay(现金分), balance, ratio, minSelfPay, fenPerPoint}
    """
    order_amount_fen = int(order_amount_fen or 0)
    rule = get_deduct_rule(activity_ref)
    wallet = get_wallet(user_id)
    fpp = _fpp(rule)

    if order_amount_fen <= 0:
        return {'points': 0, 'valueFen': 0, 'selfPay': max(order_amount_fen, 0),
                'balance': wallet.balance, 'ratio': float(rule.max_deduct_ratio),
                'minSelfPay': rule.min_self_pay, 'fenPerPoint': fpp}

    balance_value = int(wallet.balance * fpp)  # 余额可抵扣的分上限
    candidates = [
        balance_value,
        int(order_amount_fen * float(rule.max_deduct_ratio)),
        max(order_amount_fen - rule.min_self_pay, 0),  # 保证实付 ≥ 最低自付
    ]
    if rule.per_order_cap:
        candidates.append(int(rule.per_order_cap))
    if rule.daily_deduct_cap:
        used_fen = _today_deduct_used_points(wallet.id) * fpp
        candidates.append(max(int(rule.daily_deduct_cap) - int(used_fen), 0))

    deduct_fen = max(min(candidates), 0)
    points = int(deduct_fen // fpp)                    # 取整到可抵扣的整积分
    value_fen = int(round(points * fpp))               # 该整数积分对应的实际抵扣分
    return {
        'points': points,
        'valueFen': value_fen,
        'selfPay': order_amount_fen - value_fen,
        'balance': wallet.balance,
        'ratio': float(rule.max_deduct_ratio),
        'minSelfPay': rule.min_self_pay,
        'fenPerPoint': fpp,
    }


@transaction.atomic
def freeze_deduct(user_id, points, ref_no):
    """下单抵扣冻结：balance -> frozen（积分）。points<=0 直接返回。"""
    points = int(points or 0)
    if points <= 0:
        return get_wallet(user_id)
    wallet = get_wallet(user_id)
    if wallet.balance < points:
        raise BizError('积分余额不足', code=ErrorCode.CREDIT_INSUFFICIENT)
    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        balance=F('balance') - points,
        frozen=F('frozen') + points,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_FREEZE,
        amount=-points, ref_no=ref_no, balance_after=wallet.balance,
        remark='下单抵扣冻结',
    )
    return wallet


@transaction.atomic
def settle_deduct(user_id, points, ref_no):
    """支付成功核销：frozen 正式扣减（balance 已在冻结时减）。"""
    points = int(points or 0)
    if points <= 0:
        return get_wallet(user_id)
    wallet = get_wallet(user_id)
    if wallet.frozen < points:
        raise BizError('冻结积分异常', code=ErrorCode.CREDIT_INSUFFICIENT)
    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        frozen=F('frozen') - points,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_USE_OK,
        amount=0, ref_no=ref_no, balance_after=wallet.balance,
        remark='抵扣核销',
    )
    return wallet


@transaction.atomic
def unfreeze_deduct(user_id, points, ref_no):
    """支付失败/取消解冻：frozen -> balance。"""
    points = int(points or 0)
    if points <= 0:
        return get_wallet(user_id)
    wallet = get_wallet(user_id)
    if wallet.frozen < points:
        return wallet  # 幂等：已解冻则跳过
    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        balance=F('balance') + points,
        frozen=F('frozen') - points,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_UNFREEZE,
        amount=points, ref_no=ref_no, balance_after=wallet.balance,
        remark='抵扣解冻',
    )
    return wallet


@transaction.atomic
def deduct_reverse_on_refund(user_id, points, ref_no):
    """退款回冲：把该单抵扣消耗的积分退回余额（幂等：同 ref_no 已记 BIZ_DEDUCT 则跳过）。"""
    points = int(points or 0)
    if points <= 0:
        return get_wallet(user_id)
    wallet = get_wallet(user_id)
    if WalletTxn.objects.filter(
            wallet_id=wallet.id, biz_type=WalletTxn.BIZ_DEDUCT, ref_no=ref_no).exists():
        return wallet  # 已回冲，防重复退款回冲印积分
    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        balance=F('balance') + points,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_DEDUCT,
        amount=points, ref_no=ref_no, balance_after=wallet.balance,
        remark='退款回冲积分',
    )
    return wallet


def reverse_points_on_refund(order):
    """订单退款收敛处统一调用：把该单已核销抵扣的积分回冲余额（幂等、best-effort）。

    仅在订单确有积分抵扣时动作；异常吞掉不影响退款主流程（可据流水人工补）。
    """
    try:
        used = int(getattr(order, 'point_deduct', 0) or 0)
    except Exception:  # noqa: BLE001
        return None
    if used <= 0:
        return None
    try:
        return deduct_reverse_on_refund(order.user_id, used, order.order_ext_no)
    except Exception as exc:  # noqa: BLE001
        logger.error('退款回冲积分失败 order=%s err=%s', order.order_ext_no, exc)
        return None


@transaction.atomic
def expire_deduct(user_id, amount, ref_no=None):
    """积分过期作废（供定时任务调用）：balance 减少并记 BIZ_EXPIRE。"""
    amount = int(amount or 0)
    if amount <= 0:
        return get_wallet(user_id)
    wallet = get_wallet(user_id)
    amt = min(amount, wallet.balance)
    if amt <= 0:
        return wallet
    updated = Wallet.objects.filter(
        user_id=user_id, version=wallet.version,
    ).update(
        balance=F('balance') - amt,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_EXPIRE,
        amount=-amt, ref_no=ref_no, balance_after=wallet.balance,
        remark='积分过期作废',
    )
    return wallet


# ---------------------------------------------------------------------------
# 积分流水（展示）
# ---------------------------------------------------------------------------
def list_transactions(user_id, limit=50):
    """积分流水列表（展示用，带文案/方向）。"""
    wallet = get_wallet(user_id)
    txns = (WalletTxn.objects.filter(wallet_id=wallet.id)
            .exclude(biz_type=WalletTxn.BIZ_FREEZE)   # 冻结/解冻/核销属内部账务，明细只展示进出
            .exclude(biz_type=WalletTxn.BIZ_UNFREEZE)
            .exclude(biz_type=WalletTxn.BIZ_USE_OK)
            .order_by('-created_at')[:limit])
    out = []
    for t in txns:
        income = t.amount > 0
        out.append({
            'bizType': t.biz_type, 'scene': t.scene,
            'title': t.remark or _SCENE_TEXT.get(t.scene, '积分变动'),
            'amount': t.amount, 'income': income,
            'balanceAfter': t.balance_after, 'createdAt': t.created_at,
        })
    return out


# ---------------------------------------------------------------------------
# 遗留：分销归因 / 计佣 / 提现（DISTRIBUTOR_ENABLED 门控，默认停用）
# ---------------------------------------------------------------------------
def ensure_distributor(user_id):
    """确保用户有分销关系记录（停用态不新建，返回 None）。"""
    if not getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False):
        return Distributor.objects.filter(user_id=user_id).first()
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
    """归因绑定（停用态直接返回，不写入）。"""
    if not getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False):
        return
    if not invite_code:
        return
    try:
        inviter = Distributor.objects.get(invite_code=invite_code, status=1)
    except Distributor.DoesNotExist:
        return
    if inviter.user_id == user_id:
        return
    existing = Distributor.objects.filter(user_id=user_id).first()
    if existing and existing.parent_id:
        return
    parent_id = inviter.user_id
    grand_id = inviter.parent_id
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


def _allow_grand():
    return bool(get_deduct_rule().allow_grand)


def calc_commission(order):
    """计佣（遗留分销，差价模式）：停用态返回 []。"""
    if not getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False):
        return []
    if not order.settle_amount:
        return []
    dist = Distributor.objects.filter(user_id=order.promoter_id).first() if order.promoter_id else None
    records = []
    base = order.pay_amount
    settle = order.settle_amount
    if order.promoter_id:
        diff = max(base - settle, 0)
        if diff > 0:
            records.append(_build_record(order, order.promoter_id, 1, diff))
            if _allow_grand() and dist and dist.grand_id:
                records.append(_build_record(order, dist.grand_id, 2, diff // 2))
    return records


def _build_record(order, promoter_id, level, amount):
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
    """计佣入账（遗留）：停用态无记录。"""
    records = calc_commission(order)
    if records:
        CommissionRecord.objects.bulk_create(records, ignore_conflicts=True)
    return records


def settle_wallet(promoter_id, amount):
    """计佣入账为积分（遗留，仅供旧流程兼容；现行走 earn）。"""
    amount = int(amount or 0)
    if amount <= 0:
        return get_wallet(promoter_id)
    wallet = get_wallet(promoter_id)
    updated = Wallet.objects.filter(
        user_id=promoter_id, version=wallet.version,
    ).update(
        balance=F('balance') + amount,
        total_income=F('total_income') + amount,
        version=F('version') + 1,
    )
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_SETTLE,
        scene=WalletTxn.SCENE_ADMIN, amount=amount, balance_after=wallet.balance,
        remark='平台奖励',
    )
    return wallet


def apply_withdraw(user_id, amount, channel=1):
    """提现已停用：积分只能抵扣电影票，不对外付款。"""
    raise BizError(
        '提现已停用，积分仅可抵扣电影票',
        code=ErrorCode.WITHDRAW_DISABLED,
    )
