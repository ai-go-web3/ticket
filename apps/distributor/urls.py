from django.urls import path

from apps.distributor.views import (
    bind, summary, team, commissions, wallet, withdraw,
    points_account, points_transactions, points_quote, deduct_quote,
)

urlpatterns = [
    # 积分（现行）
    path('points/account', points_account),
    path('points/transactions', points_transactions),
    path('points/quote', points_quote),
    # 兼容别名（旧前端仍在用）
    path('wallet', wallet),
    path('credit/quote', deduct_quote),
    path('withdraw', withdraw),
    # 分销遗留（停用态返回空/提示）
    path('bind', bind),
    path('summary', summary),
    path('team', team),
    path('commissions', commissions),
]
