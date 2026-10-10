from django.urls import path

from apps.adminapi import views_auth, views_dashboard, views_orders, views_recon, views_rules, views_refdata, views_recommend, views_media, views_points_mall, views_member_level

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

    # 屏2 · 规则维度参考数据（只读，供运营查/选）
    path('refdata/options', views_refdata.refdata_options),
    path('refdata/movies', views_refdata.refdata_movies),
    path('refdata/cinemas', views_refdata.refdata_cinemas),
    path('refdata/cities', views_refdata.refdata_cities),
    path('refdata/brands', views_refdata.refdata_brands),
    path('refdata/hall-types', views_refdata.refdata_hall_types),

    # 屏3 · 首页装修（本月推荐位）
    path('recommend/section', views_recommend.section_get),
    path('recommend/section/save', views_recommend.section_save),
    path('recommend/slots', views_recommend.slots_list),
    path('recommend/slots/create', views_recommend.slots_create),
    path('recommend/slots/reorder', views_recommend.slots_reorder),
    path('recommend/slots/preview', views_recommend.slots_preview),
    path('recommend/slots/<int:slot_id>', views_recommend.slots_update),
    path('recommend/slots/<int:slot_id>/toggle', views_recommend.slots_toggle),
    path('recommend/slots/<int:slot_id>/delete', views_recommend.slots_delete),

    # 积分商城（Story D）· 商品 CRUD / 上下架 / 调库存 / 兑换流水 / 看板
    path('points-mall/items', views_points_mall.items_list),
    path('points-mall/items/create', views_points_mall.items_create),
    path('points-mall/items/<int:item_id>', views_points_mall.item_detail),
    path('points-mall/items/<int:item_id>/update', views_points_mall.item_update),
    path('points-mall/items/<int:item_id>/publish', views_points_mall.item_publish),
    path('points-mall/items/<int:item_id>/unpublish', views_points_mall.item_unpublish),
    path('points-mall/items/<int:item_id>/adjust-stock', views_points_mall.item_adjust_stock),
    path('points-mall/items/<int:item_id>/delete', views_points_mall.item_delete),
    path('points-mall/redemptions', views_points_mall.redemptions),
    path('points-mall/dashboard', views_points_mall.dashboard),

    # 会员等级配置 · 成长值 → 等级 → 消费返利率（取代 settings.POINTS['TIERS']）
    path('member-levels', views_member_level.levels_list),
    path('member-levels/create', views_member_level.levels_create),
    path('member-levels/<int:level_id>', views_member_level.level_detail),
    path('member-levels/<int:level_id>/update', views_member_level.level_update),
    path('member-levels/<int:level_id>/toggle', views_member_level.level_toggle),
    path('member-levels/<int:level_id>/delete', views_member_level.level_delete),

    # 图片上传（存 MySQL）：供首页装修横版封面 / 横幅底图上传
    path('media/upload', views_media.media_upload),
]
