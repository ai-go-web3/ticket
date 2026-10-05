from django.apps import AppConfig


class AdminApiConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.adminapi'
    verbose_name = '运营后台'
