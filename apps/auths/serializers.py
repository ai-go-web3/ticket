"""auths 序列化器。"""
from rest_framework import serializers

from apps.auths.models import AppUser


class UserSerializer(serializers.ModelSerializer):
    """用户信息（前端契约：驼峰字段）。

    返回 { id, nickname, avatar, phoneMask, city, inviteCode }，
    city 为 { cityId(麻花), stdCode(国标), name }，未设置城市时为 null。
    注意：出于隐私安全，接口**不回传明文手机号**（model.phone 不序列化），
    仅返回脱敏 phoneMask；完整手机号留在服务端，在下单时由后端补为取票号。
    """
    avatar = serializers.CharField(source='avatar_url', allow_null=True)
    phoneMask = serializers.CharField(source='phone_mask', allow_null=True)
    inviteCode = serializers.SerializerMethodField()
    city = serializers.SerializerMethodField()

    class Meta:
        model = AppUser
        fields = ['id', 'nickname', 'avatar',
                  'phoneMask', 'inviteCode', 'city']

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


class UpdateProfileSerializer(serializers.Serializer):
    """用户主动完善资料：昵称（微信 type=nickname 输入框）/ 头像。"""
    nickname = serializers.CharField(max_length=64, required=False, allow_blank=True)
    avatar = serializers.CharField(max_length=512, required=False, allow_blank=True)
