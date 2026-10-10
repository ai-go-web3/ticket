"""movie-ticket 主路由。

统一前缀 /api/v1，各业务模块挂载到 /api/v1/<module>。
回调类接口（微信支付、麻花订单/纠纷回调）无需鉴权。

运营后台前端（admin-ui 构建产物）同域托管在 /ops/**，见 config/spa.py：
镜像内存在 ops_static 目录时自动挂载，本地未构建则不挂（不报错）。
"""
import os

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from config import spa

urlpatterns = [
    path('admin/', admin.site.urls),

    # 业务 API（前缀 /api/v1）
    path('api/v1/auth/', include('apps.auths.urls')),
    path('api/v1/catalog/', include('apps.catalog.urls')),
    path('api/v1/order/', include('apps.order.urls')),
    path('api/v1/pay/', include('apps.pay.urls')),
    path('api/v1/refund/', include('apps.refund.urls')),
    path('api/v1/distributor/', include('apps.distributor.urls')),
    path('api/v1/mall/', include('apps.points.urls')),
    path('api/v1/notify/', include('apps.notify.urls')),
    # B 端运营后台（与内置 /admin/ 不冲突）
    path('api/v1/admin/', include('apps.adminapi.urls')),

    # 上游回调（麻花 UP 通知，无需鉴权）
    path('api/v1/up/', include('apps.upadapter.urls')),

    # 健康检查
    path('api/health', include('apps.common.urls')),
]

# 运营后台 SPA（admin-ui 构建产物，Docker 多阶段构建拷入 /app/ops_static）。
# 目录不存在（本地未构建 / 自有服务器改走 Nginx）时不挂载。
_OPS_UI_DIR = str(getattr(settings, 'OPS_UI_DIR', '') or '')
if _OPS_UI_DIR and os.path.isdir(_OPS_UI_DIR):
    urlpatterns += [
        re_path(r'^ops/assets/(?P<path>.*)$', spa.spa_asset),
        re_path(r'^ops(?:/|$)', spa.spa_index),
    ]
