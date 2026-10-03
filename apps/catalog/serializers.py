"""catalog 序列化器。"""
from rest_framework import serializers

from apps.catalog.models import City, Movie, Cinema, Schedule

WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def fmt_want(n):
    """想看人数 → 展示文本：123000 -> 12.3万；830 -> 830。"""
    n = int(n or 0)
    if n >= 10000:
        v = n / 10000.0
        return f'{v:.1f}万'.replace('.0万', '万')
    return str(n)


def fmt_release(d):
    """上映日期 → 展示文本：2026-10-01 -> 2026年10月1日上映。"""
    if not d:
        return ''
    return f'{d.year}年{d.month}月{d.day}日上映'


class CitySerializer(serializers.ModelSerializer):
    class Meta:
        model = City
        fields = ['city_code', 'city_name', 'pinyin', 'std_code', 'hot']


class MovieSerializer(serializers.ModelSerializer):
    want_text = serializers.SerializerMethodField()
    release_text = serializers.SerializerMethodField()
    buy_tag = serializers.SerializerMethodField()
    buy_tag_type = serializers.SerializerMethodField()

    class Meta:
        model = Movie
        fields = ['id', 'up_movie_id', 'name', 'status', 'type', 'duration',
                  'rating', 'region', 'language', 'release_date', 'director',
                  'actors', 'poster_url', 'description',
                  'want_count', 'presale', 'want_text', 'release_text',
                  'buy_tag', 'buy_tag_type']

    def get_want_text(self, obj):
        return fmt_want(obj.want_count)

    def get_release_text(self, obj):
        return fmt_release(obj.release_date)

    def get_buy_tag(self, obj):
        """购票按钮文案：热映=提前购/特惠购，待映=提前购/预约；
        麻花确认当前城市已无排片的影片显示灰色「暂无排片」（仍可进详情看后续日期）。"""
        if getattr(obj, 'no_show_at', None):
            return '暂无排片'
        if obj.status == Movie.STATUS_COMING:
            return '提前购' if obj.presale else '预约'
        return '提前购' if obj.presale else '特惠购'

    def get_buy_tag_type(self, obj):
        """按钮配色：blue=提前购 pink=特惠购 orange=预约 gray=暂无排片。"""
        tag = self.get_buy_tag(obj)
        return {'提前购': 'blue', '特惠购': 'pink', '预约': 'orange', '暂无排片': 'gray'}.get(tag, 'pink')


class CinemaSerializer(serializers.ModelSerializer):
    # 区域（与 region 同义，前端筛选用）
    area = serializers.CharField(source='region', read_only=True)
    # 距离（米，整数）；由视图根据用户经纬度注入到实例的 _distance，未定位则为 null
    distance = serializers.SerializerMethodField()

    class Meta:
        model = Cinema
        fields = ['id', 'name', 'city_code', 'region', 'area', 'brand', 'address',
                  'lng', 'lat', 'phone', 'supports', 'business_status', 'distance']

    def get_distance(self, obj):
        return getattr(obj, '_distance', None)


class ScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = Schedule
        fields = ['id', 'up_schedule_id', 'cinema_id', 'movie_id', 'hall_name',
                  'start_at', 'end_at', 'show_type', 'language', 'min_price',
                  'origin_price', 'remain_seats', 'refundable', 'endorseable', 'sell_status']
