from django.urls import path

from apps.auths.views import wx_login, bind_phone, profile, update_profile

urlpatterns = [
    path('wx-login', wx_login),
    path('bind-phone', bind_phone),
    path('profile', profile),
    path('update-profile', update_profile),
]
