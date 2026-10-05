from django.urls import path

from apps.catalog.views import (
    cities, movies, movie_detail, cinemas, cinema_areas, cinema_brands,
    schedules, schedule_seats, coming_calendar, sync_movies, home_recommends,
)

urlpatterns = [
    path('cities', cities),
    path('movies', movies),
    path('movies/<int:movie_id>', movie_detail),
    path('coming-calendar', coming_calendar),
    path('home-recommends', home_recommends),
    path('cinemas', cinemas),
    path('cinema-areas', cinema_areas),
    path('cinema-brands', cinema_brands),
    path('schedules', schedules),
    path('schedules/<int:schedule_id>/seats', schedule_seats),
    # 运维：供微信云托管定时任务调用
    path('sync-movies', sync_movies),
]
