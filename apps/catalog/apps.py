import os
import sys

from django.apps import AppConfig
from django.conf import settings


class CatalogConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.catalog'
    verbose_name = '基础数据'

    def ready(self):
        """在 Web 服务进程内启动内置影片同步定时器（不依赖外部调用）。

        启动条件（全部满足）：
          1. settings.ENABLE_SCHEDULER 为真；
          2. 当前是常驻 Web 服务进程（runserver / gunicorn / uvicorn），
             而非 migrate/shell/test 等一次性管理命令；
          3. 若 runserver 开启 autoreload，只在真正服务的子进程（RUN_MAIN=true）启动，
             避免父进程重复起一个调度器。
        """
        if not getattr(settings, 'ENABLE_SCHEDULER', False):
            return

        argv = sys.argv
        cmd = argv[1] if len(argv) > 1 else ''
        joined = ' '.join(argv)
        is_server = (
            cmd == 'runserver'
            or 'gunicorn' in joined
            or 'uvicorn' in joined
        )
        if not is_server:
            return

        if cmd == 'runserver' and '--noreload' not in argv \
                and os.environ.get('RUN_MAIN') != 'true':
            return  # autoreload 父进程，跳过

        from apps.catalog import scheduler
        scheduler.start()
