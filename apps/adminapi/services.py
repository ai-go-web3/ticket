"""后台数据服务：看板聚合（复用 apps.order.reporting.daily_report，勿重写财务口径）。

金额：DB 里全部单位为「分」；本模块对外 KPI 统一转「元」(÷100, 2 位)，与 daily_report 口径一致。
时间：settings.USE_TZ=False，timezone.now() 返回本地naive时间，按本地日归桶。
"""
from collections import defaultdict
from datetime import timedelta

from django.db.models import Count, Sum
from django.utils import timezone

from apps.catalog.models import Cinema, Movie
from apps.order.models import TicketOrder, OrderPayment
from apps.order.reporting import daily_report, REFUND_STATUSES
from apps.refund.models import Refund


def _day_start(dt):
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def _range_start(range_key):
    """range_key: today | 7d | 30d -> (start, end) 本地日边界 [start, end)。"""
    today = _day_start(timezone.now())
    if range_key == 'today':
        return today, today + timedelta(days=1)
    days = 30 if range_key == '30d' else 7
    return today - timedelta(days=days - 1), today + timedelta(days=1)


def _yuan(fen):
    return round((fen or 0) / 100, 2)


def overview(range_key='7d'):
    """屏1 顶部 6 张 KPI。"""
    start, end = _range_start(range_key)
    base = TicketOrder.objects.filter(deleted=0, created_at__gte=start, created_at__lt=end)

    orders = base.count()
    paid = base.filter(pay_status=TicketOrder.PAY_DONE)
    gmv = paid.aggregate(v=Sum('pay_amount'))['v']

    # 实际毛利：Σ票款 − Σ结算价（仅统计已回补结算价的单）
    settled = paid.filter(settle_amount__isnull=False)
    ticket_amt = settled.aggregate(v=Sum('ticket_amount'))['v'] or 0
    settle_amt = settled.aggregate(v=Sum('settle_amount'))['v'] or 0
    profit = ticket_amt - settle_amt
    profit_rate = round(profit / gmv * 100, 2) if gmv else None

    done = base.filter(status__in=(
        TicketOrder.STATUS_DONE, TicketOrder.STATUS_SCREENED,
        TicketOrder.STATUS_WAIT_PICK)).count()
    fail = base.filter(status=TicketOrder.STATUS_DISPATCH_FAIL).count()
    denom = done + fail
    success_rate = round(done / denom * 100, 2) if denom else None

    pending_refund = base.filter(status=TicketOrder.STATUS_REFUNDING).count()

    days = 1 if range_key == 'today' else (30 if range_key == '30d' else 7)
    alerts = daily_report(days=days).get('alerts', {})
    abnormal = len(alerts.get('unsettled', [])) + len(alerts.get('inverted', []))

    return {
        'orders': orders,
        'gmv': _yuan(gmv),
        'profit': _yuan(profit),
        'profit_rate': profit_rate,
        'success_rate': success_rate,
        'pending_refund': pending_refund,
        'unsettled': len(alerts.get('unsettled', [])),
        'inverted': len(alerts.get('inverted', [])),
        'abnormal': abnormal,
    }


def trend(days=7):
    """屏1 GMV/毛利双曲线：直接复用 daily_report().days。"""
    rep = daily_report(days=days)
    return {'days': rep['days'], 'alerts': rep['alerts']}


def status_count(range_key='7d'):
    """屏1 各状态订单条数（10 态）。"""
    start, end = _range_start(range_key)
    rows = (TicketOrder.objects
            .filter(deleted=0, created_at__gte=start, created_at__lt=end)
            .values('status').annotate(n=Count('id')))
    counts = {r['status']: r['n'] for r in rows}
    order = [TicketOrder.STATUS_PAYING, TicketOrder.STATUS_DISPATCHING,
             TicketOrder.STATUS_WAIT_PICK, TicketOrder.STATUS_SCREENED,
             TicketOrder.STATUS_DONE, TicketOrder.STATUS_CLOSED,
             TicketOrder.STATUS_DISPATCH_FAIL, TicketOrder.STATUS_REFUNDING,
             TicketOrder.STATUS_REFUNDED, TicketOrder.STATUS_DISPUTE]
    labels = {10: '待付款', 20: '出票中', 30: '待取票', 35: '已放映', 40: '已完成',
              50: '已关闭', 60: '出票失败', 70: '退款中', 80: '已退款', 90: '纠纷中'}
    return [{'status': s, 'label': labels.get(s, str(s)), 'count': counts.get(s, 0)} for s in order]


def top_movies(range_key='7d', limit=5):
    """屏1 Top 影片（按已付 GMV）。"""
    start, end = _range_start(range_key)
    rows = (TicketOrder.objects
            .filter(deleted=0, pay_status=TicketOrder.PAY_DONE,
                    created_at__gte=start, created_at__lt=end)
            .exclude(status__in=REFUND_STATUSES)
            .values('movie_id').annotate(gmv=Sum('pay_amount'), n=Count('id'))
            .order_by('-gmv')[:limit])
    name_map = {m.id: m.name for m in Movie.objects.filter(id__in=[r['movie_id'] for r in rows])}
    return [{'movie_id': r['movie_id'], 'name': name_map.get(r['movie_id'], ''),
             'gmv': _yuan(r['gmv']), 'orders': r['n']} for r in rows]


def top_brands(range_key='7d', limit=5):
    """屏1 Top 院线品牌（按出票张数近似=订单数）。DB 无 brand 维度聚合，按影院分组后 python 归并品牌。"""
    start, end = _range_start(range_key)
    rows = (TicketOrder.objects
            .filter(deleted=0, pay_status=TicketOrder.PAY_DONE,
                    created_at__gte=start, created_at__lt=end)
            .exclude(status__in=REFUND_STATUSES)
            .values('cinema_id').annotate(n=Count('id'), gmv=Sum('pay_amount')))
    cinemas = {c.id: (c.brand or c.name) for c in Cinema.objects.filter(id__in=[r['cinema_id'] for r in rows])}
    agg = defaultdict(lambda: {'orders': 0, 'gmv': 0})
    for r in rows:
        brand = cinemas.get(r['cinema_id'], '未知')
        agg[brand]['orders'] += r['n']
        agg[brand]['gmv'] += r['gmv'] or 0
    ranked = sorted(agg.items(), key=lambda kv: kv[1]['orders'], reverse=True)[:limit]
    return [{'brand': k, 'orders': v['orders'], 'gmv': _yuan(v['gmv'])} for k, v in ranked]


def alerts_brief(days=7):
    """屏1 「需关注」：未结算 / 倒挂 单号列表。"""
    rep = daily_report(days=days).get('alerts', {})
    return {'unsettled': rep.get('unsettled', []), 'inverted': rep.get('inverted', [])}
