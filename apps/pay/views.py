"""pay 视图：统一下单 / 支付回调。"""
import logging

from django.http import HttpResponse
from rest_framework.decorators import api_view, permission_classes, authentication_classes
from rest_framework.permissions import AllowAny
from rest_framework import serializers

from apps.common.response import ok
from apps.pay import services

logger = logging.getLogger('app')


class PaySerializer(serializers.Serializer):
    orderId = serializers.IntegerField()


@api_view(['POST'])
def pay(request):
    """统一下单，返回 requestPayment 参数。"""
    ser = PaySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    # openid 从用户获取
    from apps.auths.models import AppUser
    user = AppUser.objects.get(id=request.user_id)
    params = services.unified_order(ser.validated_data['orderId'], user.openid)
    return ok(params)


@api_view(['POST'])
def mock_pay(request):
    """开发降级：模拟支付成功（仅未配置商户号时可用），走完支付→出票闭环。"""
    ser = PaySerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    order = services.mock_pay_success(ser.validated_data['orderId'], user_id=request.user_id)
    return ok({'orderId': order.id, 'status': order.status})


@api_view(['POST'])
@permission_classes([AllowAny])
@authentication_classes([])
def pay_callback(request):
    """微信支付回调（无需鉴权）。"""
    result = services.on_pay_callback(request)
    return HttpResponse(
        f"<xml><return_code><![CDATA[{result['return_code']}]]></return_code></xml>",
        content_type='application/xml',
    )
