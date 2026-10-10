"""积分商城服务：浏览 / 详情 / 兑换（并发安全） / 我的券包 / 积分明细。

账务根基（沿用 apps/distributor 消费积分体系）：
- 积分余额、流水在 distributor.Wallet / WalletTxn；兑换 = 一次「出账」写
  WalletTxn(biz_type=BIZ_EXCHANGE, amount=-消耗积分)。
- 兑换不可撤销：无冻结/核销两阶段，直接扣减，失败即整笔回滚（不落失败单）。

并发安全（防超卖 / 防透支，跨进程可靠）：
- 本地 LocMem 无 Redis、gunicorn 多 worker，缓存锁不可跨进程，故不依赖缓存去重。
- 采用 DB 行锁串行化同一商品的兑换：transaction.atomic 内
  select_for_update 锁 MallItem 行 → 校验库存/日限 → select_for_update 锁 Wallet 行
  → 校验余额 → F() 表达式原子扣减库存与余额。MySQL(InnoDB) 为真行级锁；
  SQLite（本地 dev）为整库写锁，同样串行安全（GET_LOCK 为 MySQL 专有，不可移植，故弃用）。
- 幂等：request_id（客户端 UUIDv4）在 MallRedemption 上 UNIQUE，重复提交返回首单结果。

锁顺序统一为「先 MallItem 后 Wallet」，所有兑换路径一致，避免交叉死锁。
"""
import logging
import uuid
from datetime import timedelta

from django.conf import settings as dj_settings
from django.db import transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.common.response import BizError, ErrorCode
from apps.distributor import services as points_services
from apps.distributor.models import Wallet, WalletTxn
from apps.points.models import MallItem, MallRedemption, Voucher

logger = logging.getLogger('app')

_CATEGORY_TEXT = MallItem.CAT_TEXT
_FEN_PER_YUAN = 100  # 分 → 元


def _fpp():
    """兑换率：1 积分抵扣多少「分」。走现行抵扣规则（默认 1，即 100 积分=1 元）。"""
    try:
        return float(points_services._fpp(points_services.get_deduct_rule()))
    except Exception:  # noqa: BLE001
        return 1.0


def _consume_rate():
    return float(dj_settings.POINTS.get('CONSUME_RATE', 0.01) or 0)


def _points_to_yuan(points):
    """把「还差 N 积分」折算成「还需消费约多少元」：points=消费返 rate 后的积分，
    反推消费额(元) = points × fpp / rate / 100。rate<=0 时返回 0（不展示引导）。"""
    rate = _consume_rate()
    if rate <= 0:
        return 0
    fen = points * _fpp() / rate
    return int(round(fen / _FEN_PER_YUAN))


def _gen_no(prefix):
    return f'{prefix}{timezone.now().strftime("%Y%m%d%H%M%S")}{uuid.uuid4().hex[:8].upper()}'


def _gen_voucher_code():
    """展示用券码：8 位大写字母数字，可读可复制。"""
    import string
    import random
    alphabet = ''.join([string.ascii_uppercase, string.digits])
    return ''.join(random.choice(alphabet) for _ in range(8))


# ---------------------------------------------------------------------------
# 浏览 / 详情
# ---------------------------------------------------------------------------
def _derive_item_view(item, balance, today_used):
    """把商品 + 用户态派生成 C 端展示字段（camelCase）。"""
    ss = item.stock_status
    shortfall = max(item.points_price - balance, 0)
    can_afford = balance >= item.points_price
    if item.daily_limit:
        quota_left = max(item.daily_limit - today_used, 0)
    else:
        quota_left = None  # 不限
    return {
        'id': item.id,
        'name': item.name,
        'category': item.category,
        'categoryText': _CATEGORY_TEXT.get(item.category, ''),
        'coverUrl': item.cover_url,
        'pointsPrice': item.points_price,
        'originPriceFen': item.origin_price_fen,
        'originPriceYuan': round(item.origin_price_fen / _FEN_PER_YUAN, 2) if item.origin_price_fen else 0,
        'worthYuan': round(item.points_price * _fpp() / _FEN_PER_YUAN, 2),  # 折算等价 ¥
        'stock': item.stock,
        'stockTotal': item.stock_total,
        'stockStatus': ss,               # normal / low / out
        'badge': item.badge,
        'expireDays': item.expire_days,
        'dailyLimit': item.daily_limit,
        'todayUsed': today_used,
        'quotaLeft': quota_left,          # null=不限
        'canAfford': can_afford,
        'shortfall': shortfall,           # 还差多少分
        'needConsumeYuan': _points_to_yuan(shortfall),  # 再消费约多少元可兑
        'sort': item.sort,
    }


def _today_used_map(user_id, category):
    """用户今日在该品类已兑换份数（用于每日上限与「今日已兑 X/N」提示）。"""
    if not user_id:
        return 0
    start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    agg = MallRedemption.objects.filter(
        user_id=user_id, category=category, created_at__gte=start,
    ).aggregate(s=Sum('quantity'))['s'] or 0
    return int(agg)


def list_items(user_id=None, category=None):
    """商城主页：仅上架未删商品，按 sort。携带用户余额 + 每卡派生态（差额/额度/库存三态）。"""
    qs = MallItem.objects.filter(status=MallItem.STATUS_ON, deleted=0)
    if category:
        qs = qs.filter(category=int(category))
    balance = 0
    if user_id:
        balance = points_services.get_wallet(user_id).balance
    items = []
    used_cache = {}
    for it in qs:
        if it.category not in used_cache:
            used_cache[it.category] = _today_used_map(user_id, it.category)
        items.append(_derive_item_view(it, balance, used_cache[it.category]))
    return {'balance': balance, 'items': items}


def item_detail(item_id, user_id=None):
    """商品详情。下架/软删对 C 端视为不存在（前端提示「该权益刚下架，看看别的」）。"""
    item = MallItem.objects.filter(id=item_id, deleted=0).first()
    if not item:
        raise BizError('权益不存在', code=ErrorCode.MALL_NOT_FOUND)
    if item.status != MallItem.STATUS_ON:
        raise BizError('该权益已下架', code=ErrorCode.MALL_ITEM_OFFLINE)
    balance = 0
    if user_id:
        balance = points_services.get_wallet(user_id).balance
    view = _derive_item_view(item, balance, _today_used_map(user_id, item.category))
    view.update({
        'description': item.description,
        'notes': item.notes,
        'applicable': item.applicable,
    })
    return view


# ---------------------------------------------------------------------------
# 兑换（并发安全 · 不可撤销）
# ---------------------------------------------------------------------------
@transaction.atomic
def redeem(user_id, item_id, request_id, quantity=1):
    """积分兑换：原子扣积分 + 扣库存 + 出兑换单 + 派生券码 + 记出账流水。

    - 幂等：同 request_id 重复提交返回首单（不重复扣减、不重复出券）。
    - 兑换不可撤销：无两阶段冻结；任一步失败整笔 atomic 回滚，不落失败单。
    返回 {redemptionNo, voucherCodes, pointsSpent, balanceAfter, item}
    """
    quantity = max(int(quantity or 1), 1)
    if quantity > 1:
        # 首期按单份兑换，多份留 Phase 2；此处仅锁死数量口径避免歧义
        quantity = 1

    # --- 幂等快路径：已有同 request_id 直接回放首单结果 ---
    existing = MallRedemption.objects.filter(request_id=request_id).first()
    if existing:
        vouchers = list(Voucher.objects.filter(redemption_id=existing.id).values_list('voucher_no', flat=True))
        wallet = points_services.get_wallet(user_id)
        return {
            'redemptionNo': existing.redemption_no,
            'voucherCodes': vouchers,
            'pointsSpent': existing.points_price,
            'balanceAfter': wallet.balance,
            'duplicated': True,
        }

    # --- 锁 1：商品行（串行化同商品兑换，防超卖） ---
    item = MallItem.objects.select_for_update().filter(id=item_id, deleted=0).first()
    if not item:
        raise BizError('权益不存在', code=ErrorCode.MALL_NOT_FOUND)
    if item.status != MallItem.STATUS_ON:
        raise BizError('该权益刚下架，看看别的', code=ErrorCode.MALL_ITEM_OFFLINE)
    if item.stock < quantity:
        raise BizError('该权益已兑完', code=ErrorCode.MALL_STOCK_EMPTY)

    # --- 每日同品类上限 ---
    if item.daily_limit:
        start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        used = MallRedemption.objects.filter(
            user_id=user_id, category=item.category, created_at__gte=start,
        ).aggregate(s=Sum('quantity'))['s'] or 0
        if used + quantity > item.daily_limit:
            raise BizError(
                f'今天已兑换 {used} 张同类型权益，明天 00:00 重置额度',
                code=ErrorCode.MALL_DAILY_LIMIT,
            )

    # --- 锁 2：钱包行（防跨商品并发透支） ---
    points_services.get_wallet(user_id)  # 确保账户存在
    wallet = Wallet.objects.select_for_update().filter(user_id=user_id).first()
    cost = item.points_price * quantity
    if not wallet or wallet.balance < cost:
        raise BizError('积分不足，去看场电影攒积分吧', code=ErrorCode.MALL_POINT_INSUFFICIENT)

    # --- 原子扣减：余额 & 库存 ---
    Wallet.objects.filter(id=wallet.id).update(
        balance=F('balance') - cost,
        version=F('version') + 1,
    )
    updated = MallItem.objects.filter(id=item.id, stock__gte=quantity).update(
        stock=F('stock') - quantity,
    )
    if not updated:  # 双保险（行锁下理论不会命中）
        raise BizError('该权益已兑完', code=ErrorCode.MALL_STOCK_EMPTY)

    wallet.refresh_from_db()

    # --- 兑换单 ---
    redemption = MallRedemption.objects.create(
        redemption_no=_gen_no('MX'),
        request_id=request_id,
        user_id=user_id,
        item_id=item.id,
        category=item.category,
        item_name=item.name,
        points_price=cost,
        quantity=quantity,
        status=MallRedemption.STATUS_SUCCESS,
    )

    # --- 出账流水（进统一积分明细，归「消耗」） ---
    WalletTxn.objects.create(
        wallet_id=wallet.id,
        biz_type=WalletTxn.BIZ_EXCHANGE,
        amount=-cost,
        ref_no=redemption.redemption_no,
        balance_after=wallet.balance,
        remark=f'积分商城兑换 · {item.name}',
    )

    # --- 派生券码 ---
    expire_at = timezone.now() + timedelta(days=max(item.expire_days, 1))
    vouchers = []
    for _ in range(quantity):
        vouchers.append(Voucher(
            voucher_no=_gen_voucher_code(),
            redemption_id=redemption.id,
            user_id=user_id,
            item_id=item.id,
            item_name=item.name,
            category=item.category,
            status=Voucher.STATUS_UNUSED,
            expire_at=expire_at,
        ))
    Voucher.objects.bulk_create(vouchers)

    logger.info('mall redeem ok user=%s item=%s cost=%s redemption=%s',
                user_id, item.id, cost, redemption.redemption_no)
    return {
        'redemptionNo': redemption.redemption_no,
        'voucherCodes': [v.voucher_no for v in vouchers],
        'pointsSpent': cost,
        'balanceAfter': wallet.balance,
        'expireAt': expire_at,
        'item': _derive_item_view(item, wallet.balance, _today_used_map(user_id, item.category)),
    }


# ---------------------------------------------------------------------------
# 我的券包
# ---------------------------------------------------------------------------
def _voucher_view(v):
    now = timezone.now()
    status = v.status
    if status == Voucher.STATUS_UNUSED and v.expire_at < now:
        status = Voucher.STATUS_EXPIRED
    return {
        'voucherNo': v.voucher_no,
        'itemName': v.item_name,
        'category': v.category,
        'categoryText': _CATEGORY_TEXT.get(v.category, ''),
        'status': status,
        'statusText': dict(Voucher.STATUS_CHOICES).get(status, ''),
        'expireAt': v.expire_at,
        'createdAt': v.created_at,
    }


def my_vouchers(user_id, tab='unused'):
    """我的券包：按 tab 过滤。unused/used/expired/all。"""
    qs = Voucher.objects.filter(user_id=user_id)
    now = timezone.now()
    if tab == 'unused':
        qs = qs.filter(status=Voucher.STATUS_UNUSED, expire_at__gte=now)
    elif tab == 'used':
        qs = qs.filter(status=Voucher.STATUS_USED)
    elif tab == 'expired':
        qs = qs.filter(status=Voucher.STATUS_UNUSED, expire_at__lt=now) | \
             qs.filter(status=Voucher.STATUS_EXPIRED)
    items = [_voucher_view(v) for v in qs.order_by('-created_at')[:100]]
    # 未使用券里临期（<=15 天）数量，供「我的」页临期横幅（M18）
    expiring_soon = Voucher.objects.filter(
        user_id=user_id, status=Voucher.STATUS_UNUSED, expire_at__gte=now,
        expire_at__lte=now + timedelta(days=15),
    ).count()
    return {'items': items, 'expiringSoon': expiring_soon}


# ---------------------------------------------------------------------------
# 积分明细（Story C · 自包含读取，不改 distributor 展示口径）
# ---------------------------------------------------------------------------
# 展示口径：入账 BIZ_SETTLE(+) / 退款回冲 BIZ_DEDUCT(+) / 商城兑换 BIZ_EXCHANGE(-) / 过期 BIZ_EXPIRE(-)
# 内部账务（冻结/解冻/核销）不进明细。与 distributor.list_transactions 一致，另加 tab 过滤 + 日期分组。
_LEDGER_BIZ = (WalletTxn.BIZ_SETTLE, WalletTxn.BIZ_DEDUCT, WalletTxn.BIZ_EXCHANGE, WalletTxn.BIZ_EXPIRE)
_SCENE_TEXT = {
    WalletTxn.SCENE_CONSUME: '消费返积分',
    WalletTxn.SCENE_CHECKIN: '每日签到',
    WalletTxn.SCENE_TASK: '任务奖励',
    WalletTxn.SCENE_BIRTHDAY: '生日/会员日',
    WalletTxn.SCENE_ADMIN: '平台奖励',
    WalletTxn.SCENE_EXCHANGE: '兑换',
}


def points_ledger(user_id, tab='all', limit=100):
    """积分流水：tab=all|income|expense|expire。按日期分组（今日/近 7 日/更早）。"""
    wallet = points_services.get_wallet(user_id)
    qs = WalletTxn.objects.filter(wallet_id=wallet.id, biz_type__in=_LEDGER_BIZ)
    if tab == 'income':
        qs = qs.filter(biz_type=WalletTxn.BIZ_SETTLE)
    elif tab == 'expense':
        qs = qs.filter(biz_type__in=(WalletTxn.BIZ_EXCHANGE, WalletTxn.BIZ_DEDUCT))
    elif tab == 'expire':
        qs = qs.filter(biz_type=WalletTxn.BIZ_EXPIRE)

    rows = list(qs.order_by('-created_at')[:limit])
    today = timezone.now().date()
    seven = today - timedelta(days=7)

    def _grp_label(d):
        if d == today:
            return '今日'
        if d >= seven:
            return '近 7 日'
        return '更早'

    groups = []
    index = {}
    for t in rows:
        d = t.created_at.date()
        label = _grp_label(d)
        if label not in index:
            index[label] = {'group': label, 'items': []}
            groups.append(index[label])
        income = t.amount > 0
        index[label]['items'].append({
            'bizType': t.biz_type,
            'title': t.remark or _SCENE_TEXT.get(t.scene, '积分变动'),
            'amount': t.amount,
            'income': income,
            'balanceAfter': t.balance_after,
            'createdAt': t.created_at,
        })

    # 顶部三格：本月已获取 / 本月已消耗 / 即将过期（临期 15 天积分，M18 口径由到期任务维护）
    month_start = today.replace(day=1)
    earned = qs.filter(
        biz_type=WalletTxn.BIZ_SETTLE, created_at__date__gte=month_start,
    ).aggregate(s=Sum('amount'))['s'] or 0
    spent = qs.filter(
        biz_type__in=(WalletTxn.BIZ_EXCHANGE, WalletTxn.BIZ_DEDUCT), created_at__date__gte=month_start,
    ).aggregate(s=Sum('amount'))['s'] or 0
    return {
        'balance': wallet.balance,
        'summary': {
            'monthEarned': int(earned),
            'monthSpent': int(-spent),
            'expiringSoon': getattr(wallet, 'expiring_soon', 0) or 0,
        },
        'groups': groups,
    }
