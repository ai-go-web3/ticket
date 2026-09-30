from django.urls import path

from apps.distributor.views import bind, summary, team, commissions, wallet, withdraw

urlpatterns = [
    path('bind', bind),
    path('summary', summary),
    path('team', team),
    path('commissions', commissions),
    path('wallet', wallet),
    path('withdraw', withdraw),
]
