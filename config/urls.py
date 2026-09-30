"""movie-ticket 主路由。

统一前缀 /api/v1，各业务模块挂载到 /api/v1/<module>。
回调类接口（微信支付、麻花订单/纠纷回调）无需鉴权。
"""
from django.contrib import admin
from django.urls import path, include

urlpatterns = [
    path('admin/', admin.site.urls),

    # 业务 API（前缀 /api/v1）
    path('api/v1/auth/', include('apps.auths.urls')),
    path('api/v1/catalog/', include('apps.catalog.urls')),
    path('api/v1/seat/', include('apps.seat.urls')),
    path('api/v1/order/', include('apps.order.urls')),
    path('api/v1/pay/', include('apps.pay.urls')),
    path('api/v1/refund/', include('apps.refund.urls')),
    path('api/v1/distributor/', include('apps.distributor.urls')),

    # 上游回调（麻花 UP 通知，无需鉴权）
    path('api/v1/up/', include('apps.upadapter.urls')),

    # 健康检查
    path('api/health', include('apps.common.urls')),
]
