"""auths 视图。"""
import logging

import requests
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny

from apps.auths.models import AppUser
from apps.auths.authentication import gen_token
from apps.auths.serializers import WxLoginSerializer, BindPhoneSerializer, UserSerializer, UpdateProfileSerializer
from apps.auths.wechat import code2session, get_phone
from apps.common.response import ok, BizError, ErrorCode

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
    except requests.RequestException as e:
        logger.error('code2session 网络异常: %s', e)
        raise BizError('微信接口暂时不可达，请稍后重试', code=ErrorCode.UP_ERROR)

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
        # 手机号换取失败(如 code 被复用微信返 40163 / 本地未配 WX_APPID 走 dev-fallback)
        # 属业务失败，用专用码，勿用 40100，否则前端会误判「登录过期」而清 token + 静默重登
        raise BizError(str(e) or '手机号绑定失败，请重试', code=ErrorCode.PHONE_BIND_FAILED)
    except requests.RequestException as e:
        logger.error('get_phone 网络异常: %s', e)
        raise BizError('微信接口暂时不可达，请稍后重试', code=ErrorCode.UP_ERROR)

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


@api_view(['POST'])
def update_profile(request):
    """用户主动完善资料：保存昵称（微信 type=nickname 输入框）/ 头像。仅更新非空字段。"""
    ser = UpdateProfileSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    user = request.user
    data = ser.validated_data
    fields = []
    nickname = (data.get('nickname') or '').strip()
    avatar = (data.get('avatar') or '').strip()
    if nickname:
        user.nickname = nickname[:64]
        fields.append('nickname')
    if avatar:
        user.avatar_url = avatar[:512]
        fields.append('avatar_url')
    if fields:
        user.save(update_fields=fields)
    return ok(UserSerializer(user).data)
