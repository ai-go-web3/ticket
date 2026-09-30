"""auths 序列化器。"""
from rest_framework import serializers

from apps.auths.models import AppUser


class UserSerializer(serializers.ModelSerializer):
    """用户信息（前端契约：驼峰字段）。

    返回 { id, nickname, avatar, phone, phoneMask, city, inviteCode }，
    city 为 { cityId(麻花), stdCode(国标), name }，未设置城市时为 null。
    """
    avatar = serializers.CharField(source='avatar_url', allow_null=True)
    phoneMask = serializers.CharField(source='phone_mask', allow_null=True)
    inviteCode = serializers.SerializerMethodField()
    city = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ['id', 'nickname', 'avatar', 'phone', 'phoneMask',
                  'inviteCode', 'city']

    def get_inviteCode(self, obj):
        # 用用户 ID 稳定生成推广码（去除易混字符的 32 进制编码，前端展示/分享用）
        alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
        n = obj.id
        code = ''
        while n > 0:
            n -= 1  # 0 起始，id=1 -> 第一个字符 '2'
            code = alphabet[n % len(alphabet)] + code
            n //= len(alphabet)
        return code or '2'

    def get_city(self, obj):
        """返回当前城市 { cityId, stdCode, name }。"""
        if not obj.city_code:
            return None
        from apps.catalog.city_mapping import mahua_to_std, city_name
        return {
            'cityId': obj.city_code,
            'stdCode': mahua_to_std(obj.city_code) or '',
            'name': city_name(obj.city_code) or '',
        }


class WxLoginSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=128, help_text='wx.login code')


class BindPhoneSerializer(serializers.Serializer):
    code = serializers.CharField(max_length=128, help_text='getPhoneNumber code')
