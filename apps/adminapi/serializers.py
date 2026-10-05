"""后台序列化器。"""
from rest_framework import serializers

from apps.catalog.models import MarkupRule
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
                  'movie_ids', 'brands', 'city_codes', 'hall_types', 'weekday_in',
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
