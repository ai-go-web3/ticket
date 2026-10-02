"""order 序列化器。"""
from rest_framework import serializers

from apps.order.models import TicketOrder, Ticket


class OrderSerializer(serializers.ModelSerializer):
    seats = serializers.SerializerMethodField()
    movieName = serializers.SerializerMethodField()
    poster = serializers.SerializerMethodField()
    cinemaName = serializers.SerializerMethodField()
    showTime = serializers.SerializerMethodField()
    statusText = serializers.SerializerMethodField()
    statusColor = serializers.SerializerMethodField()
    payRemainSeconds = serializers.SerializerMethodField()

    class Meta:
        model = TicketOrder
        fields = ['id', 'order_ext_no', 'schedule_id', 'cinema_id', 'movie_id',
                  'seats', 'seat_count', 'ticket_amount', 'service_fee',
                  'discount_amount', 'pay_amount', 'settle_amount', 'mobile',
                  'status', 'statusText', 'statusColor', 'pay_status',
                  'payRemainSeconds',
                  'movieName', 'poster', 'cinemaName', 'showTime', 'created_at']

    _STATUS_TEXT = {
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

    def get_seats(self, obj):
        import json
        try:
            seats = json.loads(obj.seats_json)
        except Exception:
            return []
        return [s.get('name') or f"{s.get('row')}排{int(s.get('col', 0)) + 1}座" for s in seats]

    def _movie(self, obj):
        cache = self.context.setdefault('_movie_cache', {})
        if obj.movie_id not in cache:
            from apps.catalog.models import Movie
            cache[obj.movie_id] = Movie.objects.filter(id=obj.movie_id).first()
        return cache[obj.movie_id]

    def get_movieName(self, obj):
        m = self._movie(obj)
        return m.name if m else ''

    def get_poster(self, obj):
        m = self._movie(obj)
        return (m.poster_url or '') if m else ''

    def get_cinemaName(self, obj):
        from apps.catalog.models import Cinema
        c = Cinema.objects.filter(id=obj.cinema_id).first()
        return c.name if c else ''

    def get_showTime(self, obj):
        from apps.catalog.models import Schedule
        s = Schedule.objects.filter(id=obj.schedule_id).first()
        if not s:
            return ''
        return s.start_at.strftime('%m-%d %H:%M')

    def get_statusText(self, obj):
        return self._STATUS_TEXT.get(obj.status, '')

    def get_statusColor(self, obj):
        # 与前端颜色约定：品红=待取票/出票中，橙=待付款，灰=已关闭/退款
        if obj.status in (TicketOrder.STATUS_PAYING,):
            return '#ff9500'
        if obj.status in (TicketOrder.STATUS_DISPATCHING, TicketOrder.STATUS_WAIT_PICK,
                          TicketOrder.STATUS_DONE):
            return '#ff2f6d'
        return '#8a90a0'

    def get_payRemainSeconds(self, obj):
        """待付款剩余支付秒数（前端倒计时用）；非待付款状态返回 0。"""
        if obj.status != TicketOrder.STATUS_PAYING:
            return 0
        from django.utils import timezone
        from apps.order.services import pay_timeout_seconds
        remain = pay_timeout_seconds() - (timezone.now() - obj.created_at).total_seconds()
        return max(int(remain), 0)


class TicketSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ['seat_no', 'ticket_code', 'barcode', 'status']
