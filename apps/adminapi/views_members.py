"""会员管理 · 查看 + 筛选 + 冻结/解冻（只读为主，聚合多模型）。

聚合来源：
- apps.auths.AppUser     身份 / 账号状态（status 1正常 2冻结；deleted=1 视为已注销）
- apps.distributor.Wallet        可用积分(balance) / 成长值(growth) / 等级(tier)
- apps.distributor.WalletTxn     积分流水（明细展示，排除冻结/解冻/核销内部账务）
- apps.distributor.MemberLevel   等级名 & 展示色（tier → name/color）
- apps.order.TicketOrder         历史订单数 + 累计消费（pay_amount 合计）

口径说明（如运营需调整，改本文件顶部常量即可）：
- 历史订单数 order_count = 该用户全部未删除订单（deleted=0）。
- 累计消费 total_spent_fen = 有效消费订单 pay_amount 合计（见 CONSUMED_STATUSES）。
- 状态三档：正常 / 已冻结 / 已注销（deleted=1）。
"""
from datetime import timedelta

from django.db.models import Count, Q, Sum
from django.utils.dateparse import parse_date
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.permissions import IsAdmin
from apps.auths.models import AppUser
from apps.catalog.models import Cinema, Movie
from apps.common.response import ok
from apps.distributor.models import MemberLevel, Wallet, WalletTxn
from apps.order.models import TicketOrder

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)

# 计入「累计消费」的有效订单状态：出票中 / 待取票 / 已放映 / 已完成。
# 排除：待付款(未付)、已关闭、出票失败、退款中/已退款、纠纷中。
CONSUMED_STATUSES = (
    TicketOrder.STATUS_DISPATCHING,
    TicketOrder.STATUS_WAIT_PICK,
    TicketOrder.STATUS_SCREENED,
    TicketOrder.STATUS_DONE,
)

STATUS_LABELS = {1: '正常', 2: '已冻结', 0: '已注销'}

# 订单状态文案（TicketOrder 模型本身无 STATUS_TEXT，仅 C 端序列化器有，这里独立定义供详情展示）
ORDER_STATUS_LABELS = {
    TicketOrder.STATUS_PAYING: '待付款',
    TicketOrder.STATUS_DISPATCHING: '出票中',
    TicketOrder.STATUS_WAIT_PICK: '待取票',
    TicketOrder.STATUS_SCREENED: '已放映',
    TicketOrder.STATUS_DONE: '已完成',
    TicketOrder.STATUS_CLOSED: '已关闭',
    TicketOrder.STATUS_DISPATCH_FAIL: '出票失败',
    TicketOrder.STATUS_REFUNDING: '退款中',
    TicketOrder.STATUS_REFUNDED: '已退款',
    TicketOrder.STATUS_DISPUTE: '纠纷中',
}

# 积分流水展示：排除内部账务（冻结/解冻/核销），与 C 端明细口径一致
_TXN_EXCLUDE_BIZ = (WalletTxn.BIZ_FREEZE, WalletTxn.BIZ_UNFREEZE, WalletTxn.BIZ_USE_OK)

_BIZ_TEXT = {
    WalletTxn.BIZ_DEDUCT: '退款回冲',
    WalletTxn.BIZ_EXPIRE: '积分过期作废',
    WalletTxn.BIZ_EXCHANGE: '积分商城兑换',
}
_SCENE_TEXT = {
    WalletTxn.SCENE_CONSUME: '消费返积分',
    WalletTxn.SCENE_CHECKIN: '每日签到',
    WalletTxn.SCENE_TASK: '任务奖励',
    WalletTxn.SCENE_BIRTHDAY: '生日/会员日',
    WalletTxn.SCENE_ADMIN: '平台奖励',
    WalletTxn.SCENE_EXCHANGE: '兑换',
}


def _txn_desc(txn):
    if txn.remark:
        return txn.remark
    if txn.biz_type == WalletTxn.BIZ_SETTLE:
        return _SCENE_TEXT.get(txn.scene, '积分入账')
    return _BIZ_TEXT.get(txn.biz_type, '积分变动')


def _mask_openid(openid):
    if not openid:
        return ''
    if len(openid) <= 10:
        return openid[:2] + '****'
    return openid[:4] + '****' + openid[-4:]


def _level_map():
    """tier(level序号) → {name, color}，含停用档（历史用户可能挂在停用等级）。"""
    return {lv.level: {'name': lv.name, 'color': lv.color or '#9aa0b0'}
            for lv in MemberLevel.objects.all()}


def _apply_filters(request):
    """把 status 三档 + tier + 时间范围 + kw 映射到 AppUser 查询集。

    status：'1' 正常 / '2' 冻结 / '0' 已注销；空则全部（含注销）。
    """
    qp = request.query_params
    status_v = qp.get('status')
    if status_v == '0':
        qs = AppUser.objects.filter(deleted=1)
    elif status_v in ('1', '2'):
        qs = AppUser.objects.filter(deleted=0, status=int(status_v))
    else:
        qs = AppUser.objects.all()  # 全部（含注销）

    # 等级筛选：先取该 tier 的 user_id 集合（Wallet.tier）
    tier = qp.get('tier')
    if tier not in (None, ''):
        uid_in_tier = list(Wallet.objects.filter(tier=int(tier)).values_list('user_id', flat=True))
        qs = qs.filter(id__in=uid_in_tier)

    d_from, d_to = qp.get('date_from'), qp.get('date_to')
    if d_from:
        dt = parse_date(d_from)
        if dt:
            qs = qs.filter(created_at__gte=dt)
    if d_to:
        dt = parse_date(d_to)
        if dt:
            qs = qs.filter(created_at__lt=dt + timedelta(days=1))

    kw = (qp.get('kw') or '').strip()
    if kw:
        qs = qs.filter(Q(phone__icontains=kw) | Q(phone_mask__icontains=kw) | Q(nickname__icontains=kw))

    return qs


def _wallet_map(user_ids):
    return {w.user_id: w for w in Wallet.objects.filter(user_id__in=user_ids)}


def _order_agg(user_ids):
    """一次聚合出每个用户的历史订单数 + 累计消费（有效状态 pay_amount 合计）。"""
    agg = TicketOrder.objects.filter(user_id__in=user_ids, deleted=0).values('user_id').annotate(
        cnt=Count('id'),
        spent=Sum('pay_amount', filter=Q(status__in=CONSUMED_STATUSES)),
    )
    return {r['user_id']: (r['cnt'], r['spent'] or 0) for r in agg}


def _brief_row(u, wallet, cnt, spent, lv_map):
    tier = getattr(wallet, 'tier', 0) or 0
    lv = lv_map.get(tier, {'name': f'LV{tier}', 'color': '#9aa0b0'})
    # 状态文案：deleted=1 → 已注销，否则按 status
    st = 0 if u.deleted == 1 else u.status
    return {
        'id': u.id,
        'nickname': u.nickname,
        'avatar_url': u.avatar_url or '',
        'phone_mask': u.phone_mask or (u.phone and u.phone[:3] + '****' + u.phone[-4:]) or '',
        'city_code': u.city_code or '',
        'status': st,
        'status_text': STATUS_LABELS.get(st, '未知'),
        'tier': tier,
        'tier_name': lv['name'],
        'tier_color': lv['color'],
        'points_balance': getattr(wallet, 'balance', 0) or 0,
        'growth': getattr(wallet, 'growth', 0) or 0,
        'order_count': cnt,
        'total_spent_fen': spent,
        'created_at': u.created_at,
        'last_login_at': u.last_login_at,
    }


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def members(request):
    """会员列表（分页 + 聚合积分/等级/订单统计）。"""
    qs = _apply_filters(request).order_by('-created_at')
    try:
        offset = max(int(request.query_params.get('offset', 0)), 0)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = min(max(int(request.query_params.get('limit', 20)), 1), 100)
    except (TypeError, ValueError):
        limit = 20

    total = qs.count()
    rows = list(qs[offset:offset + limit])
    ids = [u.id for u in rows]
    wallets = _wallet_map(ids)
    orders = _order_agg(ids)
    lv_map = _level_map()

    items = [_brief_row(u, wallets.get(u.id), *(orders.get(u.id, (0, 0))), lv_map) for u in rows]
    return ok({'total': total, 'offset': offset, 'limit': limit, 'items': items})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def members_status_count(request):
    """顶部各账号状态胶囊条数（与列表同筛选，但忽略 status 本身）。"""
    # 复用筛选但不带 status：临时移除 status 参数
    qp = request.query_params.copy()
    qp.pop('status', None)

    def _base():
        conds = {}
        if qp.get('date_from'):
            dt = parse_date(qp['date_from'])
            if dt:
                conds['created_at__gte'] = dt
        if qp.get('date_to'):
            dt = parse_date(qp['date_to'])
            if dt:
                conds['created_at__lt'] = dt + timedelta(days=1)
        kw = (qp.get('kw') or '').strip()
        q = Q()
        if kw:
            q = Q(phone__icontains=kw) | Q(phone_mask__icontains=kw) | Q(nickname__icontains=kw)
        return q, conds

    q, conds = _base()
    total = AppUser.objects.filter(q, **conds).count()
    c_normal = AppUser.objects.filter(q, deleted=0, status=1, **conds).count()
    c_frozen = AppUser.objects.filter(q, deleted=0, status=2, **conds).count()
    c_closed = AppUser.objects.filter(deleted=1, **conds).filter(q).count()
    out = [
        {'status': 1, 'label': STATUS_LABELS[1], 'count': c_normal},
        {'status': 2, 'label': STATUS_LABELS[2], 'count': c_frozen},
        {'status': 0, 'label': STATUS_LABELS[0], 'count': c_closed},
    ]
    return ok({'total': total, 'items': out})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def member_detail(request, member_id):
    """会员详情：基本信息 + 最近订单 + 积分流水（明文手机仅后台可见）。"""
    u = AppUser.objects.filter(id=member_id).first()
    if not u:
        return ok(None, msg='会员不存在')

    w = Wallet.objects.filter(user_id=u.id).first()
    tier = getattr(w, 'tier', 0) or 0
    lv_map = _level_map()
    lv = lv_map.get(tier, {'name': f'LV{tier}', 'color': '#9aa0b0'})
    st = 0 if u.deleted == 1 else u.status

    agg = _order_agg([u.id])
    cnt, spent = agg.get(u.id, (0, 0))

    # 最近订单（最新 5 笔，任意状态）
    recent = []
    for o in TicketOrder.objects.filter(user_id=u.id, deleted=0).order_by('-created_at')[:5]:
        m = Movie.objects.filter(id=o.movie_id).values_list('name', flat=True).first() or ''
        c = Cinema.objects.filter(id=o.cinema_id).values_list('name', flat=True).first() or ''
        recent.append({
            'id': o.id,
            'order_ext_no': o.order_ext_no,
            'movie_name': m,
            'cinema_name': c,
            'show_time': _fmt_show(o),
            'pay_amount_fen': o.pay_amount or 0,
            'status': o.status,
            'status_text': ORDER_STATUS_LABELS.get(o.status, f'状态{o.status}'),
            'created_at': o.created_at,
        })

    # 最近积分流水（排除内部账务，最新 20 条）；WalletTxn 通过 wallet_id 关联，无钱包则跳过
    txns = []
    if w is not None:
        for t in (WalletTxn.objects.filter(wallet_id=w.id)
                  .exclude(biz_type__in=_TXN_EXCLUDE_BIZ)
                  .order_by('-created_at')[:20]):
            txns.append({
                'id': t.id,
                'desc': _txn_desc(t),
                'biz_type': str(t.biz_type),
                'amount': t.amount,
                'balance_after': t.balance_after,
                'created_at': t.created_at,
            })

    inviter_name = None
    if u.inviter_id:
        inviter_name = AppUser.objects.filter(id=u.inviter_id).values_list('nickname', flat=True).first()

    data = _brief_row(u, w, cnt, spent, lv_map)
    data.update({
        'openid': _mask_openid(u.openid),
        'phone': u.phone or '',
        'inviter_id': u.inviter_id,
        'inviter_name': inviter_name,
        'recent_orders': recent,
        'points_txns': txns,
    })
    return ok(data)


def _fmt_show(order):
    """放映时间展示（MM-DD HH:MM）；复用 order.services.order_show_start_at，失败回退空串。"""
    try:
        from apps.order.services import order_show_start_at
        start_at = order_show_start_at(order)
        return start_at.strftime('%m-%d %H:%M') if start_at else ''
    except Exception:  # noqa: BLE001 展示兜底，不因辅助字段异常影响详情
        return ''


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def member_freeze(request, member_id):
    """冻结会员：status → 2。已注销账户不可冻结。"""
    u = AppUser.objects.filter(id=member_id, deleted=0).first()
    if not u:
        return ok(None, msg='会员不存在或已注销')
    if u.status == 2:
        return ok({'id': u.id, 'status': 2}, msg='该会员已是冻结状态')
    u.status = 2
    u.save(update_fields=['status', 'updated_at'])
    return ok({'id': u.id, 'status': 2}, msg='已冻结')


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def member_unfreeze(request, member_id):
    """解冻会员：status → 1。"""
    u = AppUser.objects.filter(id=member_id, deleted=0).first()
    if not u:
        return ok(None, msg='会员不存在或已注销')
    if u.status == 1:
        return ok({'id': u.id, 'status': 1}, msg='该会员已是正常状态')
    u.status = 1
    u.save(update_fields=['status', 'updated_at'])
    return ok({'id': u.id, 'status': 1}, msg='已解冻')
