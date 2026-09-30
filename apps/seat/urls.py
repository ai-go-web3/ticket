from django.urls import path

from apps.seat.views import lock, release

urlpatterns = [
    path('lock', lock),
    path('release', release),
]
