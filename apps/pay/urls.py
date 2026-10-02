from django.urls import path

from apps.pay.views import pay, pay_callback, mock_pay, refund_notify

urlpatterns = [
    path('unified', pay),
    path('callback', pay_callback),
    path('refund-notify', refund_notify),   # 微信退款结果通知
    path('mock', mock_pay),
]
