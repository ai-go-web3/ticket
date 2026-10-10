from django.urls import path

from apps.upadapter.views import (
    order_callback, dispute_callback, film_callback, schedule_callback,
    ocr_recognize, ocr_resolve,
)

urlpatterns = [
    path('callback/order', order_callback),          # 订单回调
    path('callback/dispute', dispute_callback),      # 纠纷回调
    path('callback/film', film_callback),            # 电影增量回调（配置地址后加 /film）
    path('callback/cinemaschedules/batch', schedule_callback),  # 排片批量回调
    # OCR 图片识别报价（C 端，须登录态；见 PROTECTED_PREFIXES '/api/v1/up/ocr'）
    path('ocr/recognize', ocr_recognize),            # POST /api/v1/up/ocr/recognize
    path('ocr/resolve', ocr_resolve),                # POST /api/v1/up/ocr/resolve
]
