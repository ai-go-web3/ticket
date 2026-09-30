"""本地开发用 settings：继承生产配置，仅把数据库换成 SQLite、缓存换成本地内存，
使后端无需远程 MySQL / 本地 Redis 即可跑通「小程序 → Django → 麻花」中转链路。

用法：python manage.py <cmd> --settings=config.settings_dev
生产配置（config/settings.py）不受影响。
"""
from config.settings import *  # noqa: F401,F403

# 数据库：本地 SQLite（文件在 ticket/db_dev.sqlite3）
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db_dev.sqlite3',  # noqa: F405
    }
}

# 缓存 / 会话：本地内存，去掉 Redis 依赖
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'ticket-dev',
    }
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
