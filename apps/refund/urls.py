from django.urls import path

from apps.refund.views import apply_dispute, apply_refund, dispute_config, refund_detail

urlpatterns = [
    # 纠纷退票（待取票订单）——需置于 <refund_no> 通配之前
    path('dispute/config', dispute_config),
    path('dispute', apply_dispute),
    path('<refund_no>', refund_detail),
]
