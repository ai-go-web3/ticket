from django.apps import AppConfig


class NotifyConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.notify'
    verbose_name = '消息通知'
