"""notify 视图。"""
from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok
from apps.notify import services


class QuotaSerializer(serializers.Serializer):
    templateIds = serializers.ListField(child=serializers.CharField(), required=False, default=list)


@api_view(['POST'])
def record_quota(request):
    """记录一次性订阅消息授权额度：前端在用户手势里 requestSubscribeMessage 后，
    把结果为 accept 的模板 id 回传，服务端逐个 +1。

    受 /api/v1/notify/quota 保护（JWTAuthMiddleware 保证 request.user_id 存在）。
    额度采集失败不阻断支付主流程——前端 fire-and-forget。
    """
    ser = QuotaSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    recorded = services.record_quota(request.user_id, ser.validated_data['templateIds'])
    return ok({'recorded': recorded})
