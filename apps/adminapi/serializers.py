"""后台序列化器。"""
from rest_framework import serializers

from apps.catalog.models import MarkupRule, Movie, RecommendSection, RecommendSlot
from apps.order.serializers import OrderSerializer


class AdminLoginSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField()


class MarkupRuleSerializer(serializers.ModelSerializer):
    """上浮加价规则（后台 CRUD）。

    加价方式与参数须自洽：rate 模式必填 rate，flat 模式必填 flat_fen；
    维度字段留空即「不限」。version 由后端乐观锁维护，前端只读。
    """
    MODES = (MarkupRule.MODE_RATE, MarkupRule.MODE_FLAT)

    class Meta:
        model = MarkupRule
        fields = ['id', 'name', 'priority', 'is_active', 'is_fallback',
                  'movie_ids', 'cinema_ids', 'brands', 'city_codes', 'hall_types', 'weekday_in',
                  'hour_from', 'hour_to',
                  'mode', 'rate', 'flat_fen',
                  'effective_from', 'effective_to',
                  'version', 'created_at', 'updated_at']
        read_only_fields = ['id', 'version', 'created_at', 'updated_at']

    def validate(self, attrs):
        mode = attrs.get('mode', getattr(self.instance, 'mode', MarkupRule.MODE_RATE))
        if mode not in self.MODES:
            raise serializers.ValidationError({'mode': '加价方式仅支持 rate/flat'})
        if mode == MarkupRule.MODE_RATE:
            rate = attrs.get('rate', self.instance.rate if self.instance else None)
            if rate is None:
                raise serializers.ValidationError({'rate': '比例上浮需填写 rate'})
            attrs['flat_fen'] = attrs.get('flat_fen', None)
        else:  # flat
            flat = attrs.get('flat_fen', self.instance.flat_fen if self.instance else None)
            if flat is None:
                raise serializers.ValidationError({'flat_fen': '固定加价需填写 flat_fen(分/张)'})
        return attrs


class AdminOrderSerializer(OrderSerializer):
    """在 C 端订单序列化基础上补一个后台毛利字段（单位：分）。

    毛利口径与 lean.html 一致：售价(pay_amount) − 结算价(settle_amount)。
    结算价未回补（settle_amount 为 NULL）时返回 None，前端显示「待结算」。
    注意：财务/对账口径的「实际毛利」用的是票款 ticket_amount − 结算价（见 reporting.py），
    两者分母不同，列表页此处刻意用售价毛利贴合运营直觉。
    """
    profit = serializers.SerializerMethodField()

    class Meta(OrderSerializer.Meta):
        fields = OrderSerializer.Meta.fields + ['profit', 'settle_amount', 'price_rate',
                                                'est_cost_amount', 'buy_mode']

    def get_profit(self, obj):
        if obj.settle_amount is None:
            return None
        return (obj.pay_amount or 0) - obj.settle_amount


class AdminUserSerializer(serializers.ModelSerializer):
    class Meta:
        from apps.adminapi.models import AdminUser
        model = AdminUser
        fields = ['id', 'username', 'nickname', 'last_login_at']


class RecommendSectionSerializer(serializers.ModelSerializer):
    """首页「本月推荐」栏目设置（单例行，后台读写）。"""

    class Meta:
        model = RecommendSection
        fields = ['id', 'title', 'max_show', 'fallback_top4', 'updated_at']
        read_only_fields = ['id', 'updated_at']


class RecommendSlotSerializer(serializers.ModelSerializer):
    """首页推荐位（影片 / 横幅 混合轮播）后台 CRUD。

    校验口径：
      - slot_type=movie：必须给 movie_id，且该内部 Movie 存在（未软删）。
      - slot_type=banner：必须给 title（横幅主文案）。
      - badge_color 限定 pink/blue/gold。
      - city_code 留 'all' 或空=全国；否则应为麻花城市ID 字符串（不强制校验存在性，避免脏数据阻塞保存）。
      - link_type=movie 时 link_ref 可留空（自动取 movie_id）；其余 link_type 由运营自填。
    movie_name / poster_url 为派生只读字段（供列表直接展示，不入库）。
    """
    COLORS = ('pink', 'blue', 'gold')
    LINK_TYPES = (RecommendSlot.LINK_MOVIE, RecommendSlot.LINK_CINEMA,
                  RecommendSlot.LINK_COUPON, RecommendSlot.LINK_ACTIVITY,
                  RecommendSlot.LINK_NONE)

    movie_name = serializers.SerializerMethodField()
    poster_url = serializers.SerializerMethodField()

    class Meta:
        model = RecommendSlot
        fields = ['id', 'slot_type', 'movie_id', 'movie_name', 'poster_url',
                  'title', 'subtitle', 'cta', 'bg', 'image_url',
                  'badge', 'badge_color', 'city_code',
                  'link_type', 'link_ref',
                  'sort', 'enabled', 'effective_from', 'effective_to',
                  'version', 'created_at', 'updated_at']
        read_only_fields = ['id', 'version', 'created_at', 'updated_at']

    def _movie(self, obj):
        """按 movie_id 惰性解析影片，同一批序列化共享缓存（context['_movie_cache']）。"""
        if not obj.movie_id:
            return None
        cache = self.context.setdefault('_movie_cache', {})
        if obj.movie_id not in cache:
            cache[obj.movie_id] = Movie.objects.filter(
                id=obj.movie_id, deleted=0).first()
        return cache[obj.movie_id]

    def get_movie_name(self, obj):
        m = self._movie(obj)
        return m.name if m else None

    def get_poster_url(self, obj):
        m = self._movie(obj)
        return m.poster_url if m else None

    def validate(self, attrs):
        slot_type = attrs.get('slot_type', getattr(self.instance, 'slot_type', RecommendSlot.TYPE_MOVIE))
        if slot_type not in (RecommendSlot.TYPE_MOVIE, RecommendSlot.TYPE_BANNER):
            raise serializers.ValidationError({'slot_type': '仅支持 movie/banner'})

        if slot_type == RecommendSlot.TYPE_MOVIE:
            movie_id = attrs.get('movie_id', self.instance.movie_id if self.instance else None)
            if not movie_id:
                raise serializers.ValidationError({'movie_id': '影片推荐位需选择影片'})
            if not Movie.objects.filter(id=movie_id, deleted=0).exists():
                raise serializers.ValidationError({'movie_id': '影片不存在或已下线'})
        else:  # banner
            title = attrs.get('title', self.instance.title if self.instance else None)
            if not (title or '').strip():
                raise serializers.ValidationError({'title': '横幅需填写主标题'})

        color = attrs.get('badge_color', getattr(self.instance, 'badge_color', 'pink'))
        if color not in self.COLORS:
            raise serializers.ValidationError({'badge_color': '角标配色仅支持 pink/blue/gold'})

        link_type = attrs.get('link_type', getattr(self.instance, 'link_type', RecommendSlot.LINK_MOVIE))
        if link_type not in self.LINK_TYPES:
            raise serializers.ValidationError({'link_type': '不支持的跳转类型'})

        return attrs
