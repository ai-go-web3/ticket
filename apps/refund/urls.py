from django.urls import path

from apps.refund.views import apply_refund, refund_detail

urlpatterns = [
    path('<int:refund_id>', refund_detail),
]
