from django.urls import path

from apps.points.views import (
    mall_items, mall_item_detail, mall_redeem, my_vouchers, points_ledger,
)

urlpatterns = [
    # 浏览（公开，带 token 则个性化）
    path('items', mall_items),
    path('items/<int:item_id>', mall_item_detail),
    # 兑换 / 券包 / 明细（受保护，见 middleware.PROTECTED_PREFIXES）
    path('redeem', mall_redeem),
    path('vouchers', my_vouchers),
    path('ledger', points_ledger),
]
