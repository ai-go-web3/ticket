from django.urls import path

from apps.order.views import create_order, order_detail, my_orders, order_count, cancel_order
from apps.refund.views import apply_refund

urlpatterns = [
    path('', create_order),                 # POST 建单
    path('list', my_orders),                # GET 我的订单
    path('count', order_count),             # GET 四态计数
    path('<int:order_id>', order_detail),   # GET 详情
    path('<int:order_id>/cancel', cancel_order),  # POST 取消
    path('<int:order_id>/refund', apply_refund),  # POST 申请退款
]
