from django.urls import path

from apps.common.views import health

urlpatterns = [
    path('', health),
]
