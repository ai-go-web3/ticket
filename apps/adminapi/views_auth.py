"""后台登录视图（唯一免鉴权接口）。"""
import logging

from django.contrib.auth.hashers import check_password
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny

from apps.adminapi.authentication import gen_admin_token
from apps.adminapi.models import AdminUser
from apps.adminapi.serializers import AdminLoginSerializer, AdminUserSerializer
from apps.common.response import ok, BizError

logger = logging.getLogger('app')


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])   # 免 C 端 JWT 解析
def login(request):
    """账号密码登录 -> 签发 role=admin token。"""
    ser = AdminLoginSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    username = ser.validated_data['username']
    password = ser.validated_data['password']

    admin = AdminUser.objects.filter(username=username).first()
    if not admin or not check_password(password, admin.password):
        raise BizError('账号或密码错误', code=40100)
    if admin.is_active != 1:
        raise BizError('账号已停用', code=40300)

    admin.last_login_at = timezone.now()
    admin.save(update_fields=['last_login_at'])

    token = gen_admin_token(admin.id)
    return ok({'token': token, 'admin': AdminUserSerializer(admin).data})
