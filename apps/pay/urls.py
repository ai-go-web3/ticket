from django.urls import path

from apps.pay.views import pay, pay_callback, mock_pay, free_pay, refund_notify

urlpatterns = [
    path('unified', pay),
    path('callback', pay_callback),
    path('refund-notify', refund_notify),   # 微信退款结果通知
    path('mock', mock_pay),
    path('free', free_pay),                 # 0 元订单（全额代金券抵扣）支付通道
]
