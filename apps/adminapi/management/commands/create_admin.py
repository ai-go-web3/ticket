from django.core.management.base import BaseCommand
from django.contrib.auth.hashers import make_password

from apps.adminapi.models import AdminUser


class Command(BaseCommand):
    """创建/重置后台管理员。用法：
        python manage.py create_admin <username> <password> [--nickname 名字]
    """
    help = '创建后台管理员账号（单一超管自用）'

    def add_arguments(self, parser):
        parser.add_argument('username')
        parser.add_argument('password')
        parser.add_argument('--nickname', default=None)

    def handle(self, *args, **opts):
        username = opts['username']
        raw = opts['password']
        admin, created = AdminUser.objects.get_or_create(
            username=username,
            defaults={'password': make_password(raw), 'nickname': opts.get('nickname')},
        )
        if not created:
            admin.password = make_password(raw)
            if opts.get('nickname'):
                admin.nickname = opts['nickname']
            admin.is_active = 1
            admin.save()
            self.stdout.write(self.style.WARNING(f'已重置密码：{username}'))
        else:
            self.stdout.write(self.style.SUCCESS(f'已创建管理员：{username}'))
