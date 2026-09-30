from django.urls import path

from apps.pay.views import pay, pay_callback, mock_pay

urlpatterns = [
    path('unified', pay),
    path('callback', pay_callback),
    path('mock', mock_pay),
]
