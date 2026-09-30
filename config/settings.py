import os
from pathlib import Path
import time

from dotenv import load_dotenv

# 加载 .env（本地开发）
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

LOG_PATH = os.path.join(BASE_DIR, 'logs')
if not os.path.exists(LOG_PATH):
    os.mkdir(LOG_PATH)

# ===== 安全 =====
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-insecure-dev-only-change-me')
DEBUG = os.environ.get('DEBUG', 'true').lower() == 'true'
ALLOWED_HOSTS = ['*']

# ===== 应用 =====
INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    # 第三方
    'rest_framework',
    'corsheaders',
    # 业务模块
    'apps.common',         # 公共（幂等/对账/任务日志）
    'apps.auths',          # 登录态/手机号绑定
    'apps.catalog',        # 城市/影片/影院/排期
    'apps.seat',           # 座位/本地锁座
    'apps.order',          # 订单状态机
    'apps.pay',            # 微信支付
    'apps.refund',         # 退款/拦截/纠纷
    'apps.distributor',    # CPS 分销
    'apps.upadapter',      # 麻花 SPI 适配层
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'corsheaders.middleware.CorsMiddleware',
    'django.middleware.common.CommonMiddleware',
    # 'django.middleware.csrf.CsrfViewMiddleware',  # 小程序用 token 鉴权，禁用 CSRF
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.common.middleware.RequestTraceMiddleware',   # traceId + 请求日志
    'apps.common.middleware.GlobalExceptionMiddleware',  # 统一异常处理
]

ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ===== 数据库（MySQL）=====
MYSQL_ADDRESS = os.environ.get('MYSQL_ADDRESS', '127.0.0.1:3306')
_host, _port = MYSQL_ADDRESS.split(':')

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': os.environ.get('MYSQL_DATABASE', 'movie_ticket'),
        'USER': os.environ.get('MYSQL_USERNAME', 'root'),
        'PASSWORD': os.environ.get('MYSQL_PASSWORD', ''),
        'HOST': _host,
        'PORT': _port,
        'OPTIONS': {
            'charset': 'utf8mb4',
            'init_command': "SET sql_mode='STRICT_TRANS_TABLES'",
        },
        'CONN_MAX_AGE': 60,
    }
}

# ===== 缓存 / 会话（Redis）=====
# USE_REDIS：是否启用 Redis 作为缓存后端。
#   true  = 生产标准（锁座快路径、token 缓存走 Redis，需 Redis 可达）
#   false = 降级为本地内存缓存（LocMemCache），后端无需 Redis 也能跑通。
#           注意：LocMemCache 是单进程内存，gunicorn 多 worker 下各进程缓存不共享，
#           锁座会退化为「纯 DB 唯一键兜底」（防超卖仍有效），token 缓存每进程各自维护。
#           正式上线选座/高并发场景必须设为 true 并配置 Redis。
USE_REDIS = os.environ.get('USE_REDIS', 'false').lower() == 'true'

REDIS_HOST = os.environ.get('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.environ.get('REDIS_PORT', '6379'))
REDIS_DB = int(os.environ.get('REDIS_DB', '0'))
REDIS_PASSWORD = os.environ.get('REDIS_PASSWORD', '')

if USE_REDIS:
    CACHES = {
        'default': {
            'BACKEND': 'django_redis.cache.RedisCache',
            'LOCATION': f'redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}',
            'OPTIONS': {'CLIENT_CLASS': 'django_redis.client.DefaultClient'},
        }
    }
else:
    # Redis 未启用时降级为本地内存缓存，避免 cache 调用抛连接异常导致接口 500。
    CACHES = {
        'default': {
            'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
            'LOCATION': 'ticket-prod',
        }
    }

# ===== DRF =====
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.auths.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'DEFAULT_RENDERER_CLASSES': (
        'rest_framework.renderers.JSONRenderer',
    ),
    'EXCEPTION_HANDLER': 'apps.common.response.api_exception_handler',
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.LimitOffsetPagination',
    'PAGE_SIZE': 20,
}

# 微信小程序配置
WECHAT = {
    'APPID': os.environ.get('WX_APPID', ''),
    'SECRET': os.environ.get('WX_SECRET', ''),
    'MCHID': os.environ.get('WX_MCHID', ''),
    'PAY_KEY': os.environ.get('WX_PAY_KEY', ''),          # 微信支付 v2 密钥
    'MCH_CERT_SERIAL': os.environ.get('WX_MCH_CERT_SERIAL', ''),
    'MCH_PRIVATE_KEY': os.environ.get('WX_MCH_PRIVATE_KEY', ''),
    'NOTIFY_URL': os.environ.get('WX_NOTIFY_URL', ''),    # 支付回调地址
}

# 麻花电影（UP）配置
MAHUA = {
    'BASE_URL': os.environ.get('MAHUA_BASE_URL', ''),
    'APP_KEY': os.environ.get('MAHUA_APP_KEY', ''),
    'APP_SECRET': os.environ.get('MAHUA_APP_SECRET', ''),
    'TOKEN_TTL': 2 * 3600,          # token 有效期 2h
    'TOKEN_REFRESH': 30 * 60,       # 每 30min 刷新
    # 放单（dispatch）会真实扣麻花账户余额并出真票，且需要公网可达的回调地址。
    # 因此默认关闭：关闭时 dispatch() 只构造并落库真实放单报文（dry-run），不发起扣款请求。
    # 确认账户/回调就绪后，设环境变量 MAHUA_DISPATCH_ENABLED=true 才会真正调用 /put/add。
    'DISPATCH_ENABLED': os.environ.get('MAHUA_DISPATCH_ENABLED', 'false').lower() == 'true',
    # 麻花放单回调地址（必须 http 开头，公网可达）。本地开发收不到回调，靠查询轮询收敛。
    'CALLBACK_URL': os.environ.get('MAHUA_CALLBACK_URL', ''),
}

# 订单/锁座时长（秒）
SEAT_LOCK_TTL = int(os.environ.get('SEAT_LOCK_TTL', '600'))    # 锁座 10 分钟
PAY_TIMEOUT = int(os.environ.get('PAY_TIMEOUT', '900'))        # 支付 15 分钟

# JWT
JWT_EXPIRE_SECONDS = int(os.environ.get('JWT_EXPIRE_SECONDS', '7200'))

# ===== 运维/定时任务 =====
# 内部定时任务接口（如影片同步）的共享令牌。由微信云托管「定时任务」在请求头
# X-Task-Token 携带。留空则相关接口一律拒绝执行（fail-closed，防止误暴露）。
TASK_TOKEN = os.environ.get('TASK_TOKEN', '')
# 影片同步任务默认城市（麻花 cityId，成都=8）。可被接口 body 的 city 覆盖。
SYNC_DEFAULT_CITY = os.environ.get('SYNC_DEFAULT_CITY', '8')

# 密码校验
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ===== 日志 =====
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '[%(asctime)s] [%(filename)s:%(lineno)d] [%(module)s:%(funcName)s] '
                      '[%(levelname)s]- %(message)s'
        },
        'simple': {'format': '%(levelname)s %(message)s'},
    },
    'handlers': {
        'default': {
            'level': 'INFO',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_PATH, f'all-{time.strftime("%Y-%m-%d")}.log'),
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'standard',
            'encoding': 'utf-8',
        },
        'error': {
            'level': 'ERROR',
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': os.path.join(LOG_PATH, f'error-{time.strftime("%Y-%m-%d")}.log'),
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'standard',
            'encoding': 'utf-8',
        },
        'console': {
            'level': 'DEBUG',
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'django': {'handlers': ['default', 'console'], 'level': 'INFO', 'propagate': False},
        'app': {'handlers': ['error', 'console', 'default'], 'level': 'INFO', 'propagate': True},
    },
}

# ===== 国际化 =====
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_L10N = True
USE_TZ = False

STATIC_URL = '/static/'
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
