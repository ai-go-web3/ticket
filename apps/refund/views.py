"""refund 视图。"""
import logging

from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok, BizError
from apps.refund import services
from apps.refund.models import Refund, Dispute

logger = logging.getLogger('app')


class ApplyRefundSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


@api_view(['POST'])
def apply_refund(request, order_id):
    """申请退款。"""
    ser = ApplyRefundSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    refund = services.apply_refund(
        request.user_id, order_id, ser.validated_data.get('reason'),
    )
    return ok({'refundNo': refund.refund_ext_no, 'status': refund.status})


@api_view(['GET'])
def refund_detail(request, refund_id):
    """退款进度。"""
    try:
        refund = Refund.objects.get(id=refund_id)
    except Refund.DoesNotExist:
        raise BizError('退款单不存在', code=40400)
    return ok({
        'refundNo': refund.refund_ext_no,
        'status': refund.status,
        'fee': refund.fee,
        'refundAmount': refund.refund_amount,
    })
