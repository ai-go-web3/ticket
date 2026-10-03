"""财务报表服务：按日汇总盈利与对账口径。

口径（详见 docs/财务口径与对账.md）：
    实收       = Σpay_amount（不收服务费，= ΣsalePrice）
    票款       = Σticket_amount
    预估成本   = Σest_cost_amount（建单时原始 fastPrice 快照）
    实际成本   = Σsettle_amount（麻花 confirmPrice，真实出票结算）
    预估毛利   = 票款 - 预估成本
    实际毛利   = 票款 - 实际成本
    对账差异   = 预估成本 - 实际成本（正=麻花结算低于选座时成本，负=倒挂需关注）

统计范围：pay_status=已付、未删订单，按「支付时间」归日（OrderPayment 成功流水）。
退款单单列（出票失败自动退款/退票），不混入毛利。
"""
import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.order.models import TicketOrder, OrderPayment

logger = logging.getLogger('app')

REFUND_STATUSES = (TicketOrder.STATUS_REFUNDING, TicketOrder.STATUS_REFUNDED,
                   TicketOrder.STATUS_DISPATCH_FAIL)


def _pay_dates(order_ids):
    """order_id -> 支付时间（取该单最早一条成功支付流水）。"""
    rows = (OrderPayment.objects
            .filter(order_id__in=order_ids, status=OrderPayment.STATUS_SUCCESS)
            .order_by('created_at'))
    out = {}
    for p in rows:
        out.setdefault(p.order_id, p.created_at)  # USE_TZ=False，已是本地时间
    return out


def _day_start(dt):
    """当地零点（USE_TZ=False 时 Django 返回的已是本地时间，直接截断）。"""
    return dt.replace(hour=0, minute=0, second=0, microsecond=0)


def daily_report(days=7, date=None):
    """近 N 天（或指定日期）的按日财务汇总。

    date: 'YYYY-MM-DD' 指定单日；否则取最近 days 天（含今天）。
    返回 {days: [{date, orders, gmv, ticket, est_cost, settle, est_profit,
                  real_profit, diff, refunded_orders, refunded_amount}],
          alerts: {unsettled: [单号], inverted: [单号]}}
    """
    end = _day_start(timezone.now()) + timedelta(days=1)
    if date:
        start = _day_start(timezone.make_aware(
            timezone.datetime.strptime(date, '%Y-%m-%d')
        )) if settings.USE_TZ else _day_start(
            timezone.datetime.strptime(date, '%Y-%m-%d'))
        end = start + timedelta(days=1)
    else:
        start = end - timedelta(days=max(1, int(days)))

    orders = list(TicketOrder.objects.filter(
        deleted=0, pay_status=TicketOrder.PAY_DONE,
        created_at__gte=start - timedelta(days=1),  # 多取一天，支付时间可能晚于建单日
    ))
    pay_at = _pay_dates([o.id for o in orders])

    buckets = defaultdict(lambda: defaultdict(int))
    alerts = {'unsettled': [], 'inverted': []}
    refund_buckets = defaultdict(lambda: defaultdict(int))

    for o in orders:
        paid_at = pay_at.get(o.id)
        if not paid_at or not (start <= paid_at < end):
            continue
        day = paid_at.strftime('%Y-%m-%d')
        if o.status in REFUND_STATUSES:
            b = refund_buckets[day]
            b['orders'] += 1
            b['amount'] += o.pay_amount or 0
            continue
        b = buckets[day]
        b['orders'] += 1
        b['gmv'] += o.pay_amount or 0
        b['ticket'] += o.ticket_amount or 0
        b['est_cost'] += o.est_cost_amount or 0
        if o.settle_amount is not None:
            b['settle'] += o.settle_amount
        elif o.status in (TicketOrder.STATUS_DISPATCHING, TicketOrder.STATUS_DONE,
                          TicketOrder.STATUS_WAIT_PICK):
            # 已付且在出票链路中却始终没有结算价：对账缺口
            alerts['unsettled'].append(o.order_ext_no)
        # 倒挂：实际结算高于票款收入（卖价低于成本）
        if o.settle_amount is not None and o.settle_amount > (o.ticket_amount or 0):
            alerts['inverted'].append(o.order_ext_no)

    out_days = []
    cur = start
    while cur < end:
        day = cur.strftime('%Y-%m-%d')
        b = buckets[day]
        r = refund_buckets[day]
        out_days.append({
            'date': day,
            'orders': b['orders'],
            'gmv': round(b['gmv'] / 100, 2),
            'ticket': round(b['ticket'] / 100, 2),
            'est_cost': round(b['est_cost'] / 100, 2),
            'settle': round(b['settle'] / 100, 2),
            'est_profit': round((b['ticket'] - b['est_cost']) / 100, 2),
            'real_profit': round((b['ticket'] - b['settle']) / 100, 2),
            'diff': round((b['est_cost'] - b['settle']) / 100, 2),
            'refunded_orders': r['orders'],
            'refunded_amount': round(r['amount'] / 100, 2),
        })
        cur += timedelta(days=1)

    return {'days': out_days, 'alerts': alerts}
