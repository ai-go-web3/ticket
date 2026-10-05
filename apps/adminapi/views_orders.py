"""屏3 · 订单列表 + 状态计数 + 导出（只读为主）。"""
import csv
import io
from datetime import timedelta

from django.db.models import Count, Q
from django.http import StreamingHttpResponse
from django.utils.dateparse import parse_date
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.permissions import IsAdmin
from apps.adminapi.serializers import AdminOrderSerializer
from apps.catalog.models import Cinema, Movie
from apps.order.models import TicketOrder
from apps.common.response import ok

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)

EXPORT_MAX_ROWS = 50000   # 导出行数上限，防大查询压主库

STATUS_LABELS = {
    TicketOrder.STATUS_PAYING: '待付款',
    TicketOrder.STATUS_DISPATCHING: '出票中',
    TicketOrder.STATUS_WAIT_PICK: '待取票',
    TicketOrder.STATUS_DONE: '已完成',
    TicketOrder.STATUS_CLOSED: '已关闭',
    TicketOrder.STATUS_DISPATCH_FAIL: '出票失败',
    TicketOrder.STATUS_REFUNDING: '退款中',
    TicketOrder.STATUS_REFUNDED: '已退款',
    TicketOrder.STATUS_DISPUTE: '纠纷中',
}


def _apply_filters(request, include_status=True):
    qs = TicketOrder.objects.filter(deleted=0)
    qp = request.query_params

    if include_status and qp.get('status'):
        qs = qs.filter(status=qp['status'])
    if qp.get('movie_id'):
        qs = qs.filter(movie_id=qp['movie_id'])
    if qp.get('buy_mode'):
        qs = qs.filter(buy_mode=qp['buy_mode'])

    # 品牌 / 城市 -> 先取影院 id 集合再过滤（订单表只有 cinema_id）
    cfilter = {}
    if qp.get('brand'):
        cfilter['brand'] = qp['brand']
    if qp.get('city_code'):
        cfilter['city_code'] = qp['city_code']
    if cfilter:
        ids = list(Cinema.objects.filter(deleted=0, **cfilter).values_list('id', flat=True))
        qs = qs.filter(cinema_id__in=ids)

    d_from, d_to = qp.get('date_from'), qp.get('date_to')
    if d_from:
        dt = parse_date(d_from)
        if dt:
            qs = qs.filter(created_at__gte=dt)
    if d_to:
        dt = parse_date(d_to)
        if dt:
            qs = qs.filter(created_at__lt=dt + timedelta(days=1))

    kw = qp.get('kw')
    if kw:
        qs = qs.filter(Q(order_ext_no__icontains=kw) | Q(mobile__icontains=kw))

    return qs


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def orders(request):
    """订单列表（分页 + 三层价 + 毛利）。"""
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
    rows = qs[offset:offset + limit]
    return ok({'total': total, 'offset': offset, 'limit': limit,
               'items': AdminOrderSerializer(rows, many=True, context={}).data})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def orders_status_count(request):
    """顶部 9 状态胶囊条数（与列表同筛选，但忽略 status 本身）。"""
    qs = _apply_filters(request, include_status=False)
    counts = {r['status']: r['n'] for r in qs.values('status').annotate(n=Count('id'))}
    out = [{'status': s, 'label': STATUS_LABELS[s], 'count': counts.get(s, 0)}
           for s in sorted(STATUS_LABELS)]
    return ok({'total': qs.count(), 'items': out})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def order_detail(request, order_id):
    """订单详情（精简版够用：主表字段 + 三层价 + 毛利）。"""
    o = TicketOrder.objects.filter(id=order_id, deleted=0).first()
    if not o:
        return ok(None, msg='订单不存在')
    return ok(AdminOrderSerializer(o, context={}).data)


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def orders_export(request):
    """流式 CSV 导出（P2 提前实现，带行数上限）。"""
    qs = _apply_filters(request).order_by('-created_at')[:EXPORT_MAX_ROWS]
    movie_ids = set(qs.values_list('movie_id', flat=True))
    movie_map = {m.id: m.name for m in Movie.objects.filter(id__in=movie_ids)}

    header = ['订单号', '影片', '影院ID', '状态', '座位数', '票面(分)', '实付(分)',
              '结算(分)', '毛利(分)', '费率', '购票模式', '下单时间']

    def row_line(values):
        buf = io.StringIO()
        csv.writer(buf).writerow(values)
        return buf.getvalue()

    def gen():
        yield '\ufeff' + row_line(header)
        for o in qs.iterator():
            profit = (o.pay_amount or 0) - o.settle_amount if o.settle_amount is not None else ''
            yield row_line([
                o.order_ext_no, movie_map.get(o.movie_id, ''), o.cinema_id,
                STATUS_LABELS.get(o.status, o.status), o.seat_count,
                o.ticket_amount, o.pay_amount,
                '' if o.settle_amount is None else o.settle_amount, profit,
                o.price_rate, o.buy_mode, o.created_at.strftime('%Y-%m-%d %H:%M:%S'),
            ])

    resp = StreamingHttpResponse(gen(), content_type='text/csv; charset=utf-8')
    resp['Content-Disposition'] = 'attachment; filename="orders.csv"'
    return resp
