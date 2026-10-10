"""积分服务：账户/等级 / 消费返积分入账 / 积分兑换卡券 / 遗留分销计佣（停用）。

现行方向 = 消费积分体系（合规：奖励只挂用户自身消费/行为，与拉人无关）：
- 获取：earn_consume(消费返积分)；后续可扩 签到/任务/生日/后台直发（scene 细分）。
- 用途：仅用于积分商城兑换卡券（apps/points），不再支持抵扣购票现金（抵扣已下线）。
- 单位：钱包 balance/frozen 与流水 amount 均为「积分」；与「分」的换算率取自
  DeductRule.fen_per_point（默认 1 积分 = 1 分，即 100 积分 = 1 元），
  现仅供消费返利折算与商城兑换定价使用。
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
    """按成长值落等级：优先读 MemberLevel 表（运营在后台配置），表空回落 settings.POINTS['TIERS']。

    返回 (level, name, rate)；rate=0 表示沿用全局 CONSUME_RATE。
    """
    from apps.distributor.models import MemberLevel

    growth = int(growth or 0)
    base_rate = float(dj_settings.POINTS.get('CONSUME_RATE', 0) or 0)

    rows = list(
        MemberLevel.objects.filter(is_active=1).order_by('growth_min').values_list(
            'level', 'name', 'growth_min', 'consume_rate',
        )
    )
    if rows:
        hit = (rows[0][0], rows[0][1], base_rate)  # 最低档兜底
        for level, name, gmin, rate in rows:
            if growth >= int(gmin or 0):
                r = float(rate or 0) or base_rate
                hit = (int(level), name, r)
        return hit

    # fallback：DB 表未 seed 时用 settings 硬编码（保底不炸）
    tiers = dj_settings.POINTS.get('TIERS') or []
    hit = (0, '新影迷', base_rate)
    for t in sorted(tiers, key=lambda x: x.get('growth_min', 0)):
        if growth >= int(t.get('growth_min', 0)):
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
    """积分账户概览（供积分中心页）：余额/成长值/等级 + 换算率（供商城兑换展示）。"""
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
        'expireDays': rule.expire_days,
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
def earn(order, points, scene, ref_no=None, remark=None, allow_zero=False, award_growth=False):
    """积分入账最小单元（原子 + CAS）：balance/total_income 同增；
    award_growth=True 时同步涨 growth 并重算等级（仅 SCENE_CONSUME 走这条，
    签到/任务/生日/后台直发等非观影场景不入成长值）。记流水。

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
    updates = {
        'balance': F('balance') + points,
        'total_income': F('total_income') + points,
        'version': F('version') + 1,
    }
    if award_growth:
        updates['growth'] = F('growth') + points
    updated = Wallet.objects.filter(
        user_id=wallet.user_id, version=wallet.version,
    ).update(**updates)
    if not updated:
        raise BizError('积分更新冲突，请重试')
    wallet.refresh_from_db()
    if award_growth:
        recalc_tier(wallet)
    WalletTxn.objects.create(
        wallet_id=wallet.id, biz_type=WalletTxn.BIZ_SETTLE, scene=scene,
        amount=points, ref_no=ref_no, balance_after=wallet.balance,
        remark=remark or _SCENE_TEXT.get(scene, '积分入账'),
    )
    return wallet


@transaction.atomic
def earn_consume(order):
    """消费返积分：订单置为终态「已放映」后，按订单实付现金 × 等级返利率入账。

    - 只对「自身消费」返利，与是否邀请到人无关（合规根基）；
    - 封顶：不超毛利（pay_amount − settle_amount，settle 缺失则不设该上限）、单笔上限；
    - 幂等：同单已记 SCENE_CONSUME 入账则跳过（扫描任务重复触发亦安全）。
    - 未置「已放映」（观影终点）前不入账，规避「下单返完就退票」白嫖；退款则回冲（见下）。
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
                ref_no=order.order_ext_no, remark='消费返积分', award_growth=True)


def confirm_settle(order):
    """终态(已放映)统一激励入口：先做消费返积分（现行方向）；分销计佣受开关门控（默认关）。"""
    if _points_enabled():
        try:
            earn_consume(order)
        except Exception as exc:  # noqa: BLE001 返积分失败不影响出票主流程，留痕人工补
            logger.error('消费返积分失败 order=%s err=%s', order.order_ext_no, exc)
    if getattr(dj_settings, 'DISTRIBUTOR_ENABLED', False):
        settle_commission(order)


# ---------------------------------------------------------------------------
# 积分过期作废（通用货币生命周期，与卡券兑换并存；抵扣购票已下线）
# ---------------------------------------------------------------------------
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
