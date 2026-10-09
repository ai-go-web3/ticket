"""refund 视图。"""
import logging

from rest_framework.decorators import api_view
from rest_framework import serializers

from apps.common.response import ok, BizError, ErrorCode
from apps.refund import services
from apps.refund.models import Refund, Dispute

logger = logging.getLogger('app')


class ApplyRefundSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True)


class ApplyDisputeSerializer(serializers.Serializer):
    reason = serializers.CharField()                       # 纠纷原因（取值来自 dispute/config reasons）
    content = serializers.CharField(required=False, allow_blank=True, max_length=200)


@api_view(['POST'])
def apply_refund(request, order_id):
    """申请退款（出票中订单，走麻花拦截）。"""
    ser = ApplyRefundSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    refund = services.apply_refund(
        request.user_id, order_id, ser.validated_data.get('reason'),
    )
    return ok({'refundNo': refund.refund_ext_no, 'status': refund.status})


@api_view(['GET'])
def dispute_config(request):
    """待取票纠纷退票配置：原因列表 + 预估手续费 + 是否可退（含不可退原因）。"""
    order_id = request.query_params.get('orderId')
    if not order_id:
        raise BizError('缺少订单参数', code=ErrorCode.PARAM_ERROR)
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise BizError('订单参数无效', code=ErrorCode.PARAM_ERROR)
    return ok(services.dispute_config(order_id, request.user_id))


@api_view(['POST'])
def apply_dispute(request):
    """发起纠纷退票（待取票订单，走麻花 /put/dispute）。"""
    ser = ApplyDisputeSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    order_id = request.data.get('orderId')
    try:
        order_id = int(order_id)
    except (TypeError, ValueError):
        raise BizError('订单参数无效', code=ErrorCode.PARAM_ERROR)
    refund = services.apply_dispute(
        request.user_id, order_id,
        ser.validated_data['reason'],
        ser.validated_data.get('content'),
    )
    return ok({'refundNo': refund.refund_ext_no, 'status': refund.status})


@api_view(['GET'])
def refund_detail(request, refund_no):
    """退款进度（支持退款单号 refund_ext_no 或数字ID查询）。"""
    refund = (Refund.objects.filter(refund_ext_no=refund_no).first()
              or (Refund.objects.filter(id=refund_no).first()
                  if str(refund_no).isdigit() else None))
    if refund is None:
        raise BizError('退款单不存在', code=ErrorCode.NOT_FOUND)
    dispute = Dispute.objects.filter(refund_id=refund.id).first()
    return ok({
        'refundNo': refund.refund_ext_no,
        'type': refund.type,
        'status': refund.status,
        'fee': refund.fee,
        'refundAmount': refund.refund_amount,
        'reason': refund.reason or '',
        'disputeStatus': dispute.status if dispute else None,
    })
