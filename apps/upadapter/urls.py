from django.urls import path

from apps.upadapter.views import (
    order_callback, dispute_callback, film_callback, schedule_callback,
)

urlpatterns = [
    path('callback/order', order_callback),          # 订单回调
    path('callback/dispute', dispute_callback),      # 纠纷回调
    path('callback/film', film_callback),            # 电影增量回调（配置地址后加 /film）
    path('callback/cinemaschedules/batch', schedule_callback),  # 排片批量回调
]
