"""auths 视图。"""
import logging

from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny

from apps.auths.models import AppUser
from apps.auths.authentication import gen_token
from apps.auths.serializers import WxLoginSerializer, BindPhoneSerializer, UserSerializer
from apps.auths.wechat import code2session, get_phone
from apps.common.response import ok, BizError

logger = logging.getLogger('app')


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def wx_login(request):
    """微信登录：code -> openid -> 建/查用户 -> 签发 token。"""
    ser = WxLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    code = ser.validated_data['code']

    try:
        sess = code2session(code)
    except ValueError as e:
        raise BizError(str(e), code=40100)

    openid = sess['openid']
    unionid = sess.get('unionid')

    user, created = AppUser.objects.get_or_create(
        openid=openid,
        defaults={'unionid': unionid},
    )
    if not created and unionid and not user.unionid:
        user.unionid = unionid
        user.save(update_fields=['unionid'])

    user.last_login_at = timezone.now()
    user.save(update_fields=['last_login_at'])

    token = gen_token(user.id)
    return ok({'token': token, 'userInfo': UserSerializer(user).data})


@api_view(['POST'])
def bind_phone(request):
    """绑定手机号：getPhoneNumber code -> 手机号 -> 脱敏存。"""
    ser = BindPhoneSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    code = ser.validated_data['code']

    try:
        phone_info = get_phone(code)
    except ValueError as e:
        raise BizError(str(e), code=40100)

    phone = phone_info['phone']
    mask = phone[:3] + '****' + phone[-4:]

    user = request.user
    user.phone = phone
    user.phone_mask = mask
    user.save(update_fields=['phone', 'phone_mask'])
    return ok({'phoneMask': mask})


@api_view(['GET'])
def profile(request):
    """当前用户信息。"""
    return ok(UserSerializer(request.user).data)
