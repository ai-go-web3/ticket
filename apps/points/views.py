"""积分商城 C 端视图。

鉴权口径（见 apps/common/middleware.py）：
- 浏览类 /mall/items、/mall/items/<id> 公开；带有效 token 时 request.user_id 会被注入，
  用于展示余额/差额/今日额度等个性化字段（匿名则为 0，前端展示未登录 gate）。
- 兑换 / 券包 / 明细 挂在受保护前缀下，未登录由中间件直接 401（前端触发静默重登）。

响应统一走 apps.common.response.ok({code,msg,data})。
"""
import logging
import uuid

from rest_framework.decorators import api_view

from apps.common.response import ok, BizError, ErrorCode
from apps.points import services

logger = logging.getLogger('app')


@api_view(['GET'])
def mall_items(request):
    """商城主页列表。query: category(可选)。"""
    category = request.query_params.get('category') or None
    return ok(services.list_items(getattr(request, 'user_id', None), category))


@api_view(['GET'])
def mall_item_detail(request, item_id):
    """商品详情。"""
    return ok(services.item_detail(item_id, getattr(request, 'user_id', None)))


@api_view(['POST'])
def mall_redeem(request):
    """兑换（不可撤销）。body: {itemId, requestId?, quantity?}。

    requestId 客户端生成 UUID 保证幂等；缺省时服务端补一个（同请求内幂等，
    跨请求幂等需客户端自带）。
    """
    data = request.data or {}
    item_id = data.get('itemId') or data.get('item_id')
    if not item_id:
        raise BizError('缺少 itemId', code=ErrorCode.PARAM_ERROR)
    request_id = (data.get('requestId') or data.get('request_id') or uuid.uuid4().hex)
    quantity = data.get('quantity', 1)
    result = services.redeem(request.user_id, int(item_id), str(request_id)[:64], int(quantity))
    return ok(result, msg='兑换成功')


@api_view(['GET'])
def my_vouchers(request):
    """我的券包。query: tab=unused|used|expired|all。"""
    tab = request.query_params.get('tab') or 'unused'
    return ok(services.my_vouchers(request.user_id, tab))


@api_view(['GET'])
def usable_vouchers(request):
    """确认订单页选券：可用于本单金额抵扣的代金券。query: amount(分,用券前应付)。"""
    amount = request.query_params.get('amount') or 0
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 0
    return ok(services.usable_vouchers(request.user_id, amount))


@api_view(['GET'])
def points_ledger(request):
    """积分明细。query: tab=all|income|expense|expire。"""
    tab = request.query_params.get('tab') or 'all'
    return ok(services.points_ledger(request.user_id, tab))
