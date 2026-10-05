from django.urls import path

from apps.adminapi import views_auth, views_dashboard, views_orders, views_recon, views_rules

urlpatterns = [
    # 登录（唯一免鉴权）
    path('login', views_auth.login),

    # 屏1 · 数据看板
    path('dashboard/overview', views_dashboard.overview),
    path('dashboard/trend', views_dashboard.trend),
    path('dashboard/status-count', views_dashboard.status_count),
    path('dashboard/top', views_dashboard.top),
    path('dashboard/alerts', views_dashboard.alerts),

    # 屏3 · 订单
    path('orders', views_orders.orders),
    path('orders/status-count', views_orders.orders_status_count),
    path('orders/export', views_orders.orders_export),
    path('orders/<int:order_id>', views_orders.order_detail),

    # 屏4 · 对账与盈利
    path('finance/daily', views_recon.finance_daily),
    path('finance/summary', views_recon.finance_summary),

    # 屏2 · 上浮规则（P3）
    path('rules', views_rules.rules_list),
    path('rules/create', views_rules.rules_create),
    path('rules/preview', views_rules.rules_preview),
    path('rules/<int:rule_id>', views_rules.rules_update),
    path('rules/<int:rule_id>/toggle', views_rules.rules_toggle),
    path('rules/<int:rule_id>/delete', views_rules.rules_delete),
]
