from django.urls import path

from apps.notify.views import record_quota

urlpatterns = [
    path('quota', record_quota),   # POST 记录订阅消息授权额度（需登录态）
]
