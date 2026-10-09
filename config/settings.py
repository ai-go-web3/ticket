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
    'apps.order',          # 订单状态机
    'apps.pay',            # 微信支付
    'apps.refund',         # 退款/拦截/纠纷
    'apps.distributor',    # CPS 分销
    'apps.upadapter',      # 麻花 SPI 适配层
    'apps.adminapi',       # B 端运营后台（同工程，挂 /api/v1/admin）
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
    'apps.common.middleware.JWTAuthMiddleware',        # 统一登录态校验（受保护接口 401）
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

# ===== 缓存 / 会话（进程内内存，不依赖 Redis）=====
# 全项目已去除 Redis。Django cache 用于：麻花 token 缓存、影院区域缓存、
# 同步/token 刷新去重锁、微信 access_token 缓存等，均走进程内 LocMemCache。
# 注意：LocMemCache 为单进程内存，多 worker/多副本间不共享——
#   · 座位防超卖不受影响（靠 MySQL 唯一键 seat_lock_item，非缓存）；
#   · token 缓存/同步锁变为「每进程各自维护」，建议单副本常驻，
#     或接受各进程独立刷新 token（登录频率仍远低于「每请求」）。
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
        'LOCATION': 'ticket',
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
def _resolve_path(p):
    """把证书路径解析为绝对路径：空值原样返回；已是绝对路径或含 ~ 则原样；
    相对路径拼到 BASE_DIR 下，避免依赖进程工作目录。"""
    if not p:
        return p
    if os.path.isabs(p) or p.startswith('~'):
        return p
    return str(BASE_DIR / p)

WECHAT = {
    'APPID': os.environ.get('WX_APPID', ''),
    'SECRET': os.environ.get('WX_SECRET', ''),
    'MCHID': os.environ.get('WX_MCHID', ''),
    'PAY_KEY': os.environ.get('WX_PAY_KEY', ''),          # 微信支付 v2 密钥
    'MCH_CERT_SERIAL': os.environ.get('WX_MCH_CERT_SERIAL', ''),
    'MCH_PRIVATE_KEY': os.environ.get('WX_MCH_PRIVATE_KEY', ''),
    'NOTIFY_URL': os.environ.get('WX_NOTIFY_URL', ''),    # 支付回调地址
    # 微信退款（v2 /secapi/pay/refund 需商户 API 证书做双向 TLS）：
    # apiclient_cert.pem / apiclient_key.pem 两个 PEM 文件路径（从商户平台下载的
    # 证书包解出，或用 openssl 从 apiclient_cert.p12 导出）。
    # 两者任一缺失 -> 退款降级为骨架标记（不真打款，联调环境用）。
    # 相对路径统一解析为相对 BASE_DIR 的绝对路径，避免依赖进程工作目录。
    'MCH_CERT_PATH': _resolve_path(os.environ.get('WX_MCH_CERT_PATH', '')),
    'MCH_KEY_PATH': _resolve_path(os.environ.get('WX_MCH_KEY_PATH', '')),
    # 退款结果通知地址（微信异步推送退款到账结果，AES 加密）
    'REFUND_NOTIFY_URL': os.environ.get('WX_REFUND_NOTIFY_URL', ''),
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
    # 放单总限价（元，costTotalPrice 字段）：实际出票成本高于该价时麻花拒绝出单。
    # 联调防损：设 0.01 可保证真实放单永远不成功（订单走「出票失败 -> 自动退款」，
    # 麻花余额零扣款）。留空 = 不传该字段；生产建议按业务设一个合理成本上限。
    'COST_TOTAL_PRICE': os.environ.get('MAHUA_COST_TOTAL_PRICE', ''),
}

# 订单/锁座时长（秒）
PAY_TIMEOUT = int(os.environ.get('PAY_TIMEOUT', '900'))        # 支付 15 分钟

# 放单补偿轮询间隔（秒）：STATUS_PENDING 查询/重放收敛。
# 0 = 关闭该定时任务（仍可用外部定时器调用 compensate_dispatches 替代）。
DISPATCH_SYNC_SECONDS = int(os.environ.get('DISPATCH_SYNC_SECONDS', '60'))
# 出票轮询间隔（秒）：每 2 分钟全量查询「出票中」订单收敛放单结果。
# 0 = 关闭该定时任务。
DISPATCH_QUERY_SECONDS = int(os.environ.get('DISPATCH_QUERY_SECONDS', '120'))
# 失败微信退款重试间隔（秒）。0 = 关闭该定时任务（退款单停 FAIL 待人工）。
REFUND_RETRY_SECONDS = int(os.environ.get('REFUND_RETRY_SECONDS', '300'))
# 退款失败最大自动重试次数（默认 5，每 5 分钟一次即最多重试 25 分钟）。
# 超过后退款单停留 FAIL 待人工处理。0 = 不限制重试次数。
REFUND_RETRY_MAX_TIMES = int(os.environ.get('REFUND_RETRY_MAX_TIMES', '5'))

# 待取票纠纷退票：距开场不足该分钟数（产品策略，默认 120 分钟）不显示/不允许申请退票。
# 麻花侧 rules 区间可能在发起时进一步收紧，以纠纷原因接口实时校验为准。
DISPUTE_MIN_MINUTES_BEFORE_SHOW = int(
    os.environ.get('DISPUTE_MIN_MINUTES_BEFORE_SHOW', '120'))

# 真实支付联调开关：强制所有订单实付金额（分）。
# 设 PAY_AMOUNT_OVERRIDE_FEN=1 即 0.01 元走真实微信支付/放单全链路，避免联调期
# 按票面价真金白银扣款。留空/0 = 按正常票价计费（生产必须留空或 0）。
PAY_AMOUNT_OVERRIDE_FEN = int(os.environ.get('PAY_AMOUNT_OVERRIDE_FEN', '0') or 0)

# 成本价上浮费率：麻花 fastPrice/maxSpeedPrice 是我方拿到的成本价，展示与计价前
# 统一先上浮该比例（0.05 = 上浮 5%），原价 price 为挂牌价不上浮。
# 设 0 = 不上浮，按成本价直接展示/计价。
PRICE_MARKUP_RATE = float(os.environ.get('PRICE_MARKUP_RATE', '0.05') or 0)

# ===== 消费积分体系（合规方向：取代分销返佣，奖励只挂用户自身消费/行为）=====
# 合规根基：积分不可提现/转让/折现，只能抵扣电影票；获取与「是否邀请到人」无关。
POINTS = {
    'ENABLED': os.environ.get('POINTS_ENABLED', 'true').lower() == 'true',
    # 兑换率：1 积分抵扣多少「分」。默认 1 => 100 积分 = 1 元。
    'FEN_PER_POINT': int(os.environ.get('POINT_FEN_PER_POINT', '1') or 1),
    # 消费返积分：确认收货(DONE)后，按实付金额(分) × 比例返积分。默认 1%。
    'CONSUME_RATE': float(os.environ.get('POINT_CONSUME_RATE', '0.01') or 0),
    # 单笔返积分上限（积分，0=不限）。
    'PER_ORDER_CAP': int(os.environ.get('POINT_PER_ORDER_CAP', '0') or 0),
    # 返积分是否封顶在毛利内（保证积分成本 ⊆ 毛利，不亏本）。
    'CAP_BY_MARGIN': os.environ.get('POINT_CAP_BY_MARGIN', 'true').lower() == 'true',
    # 毛利系数：返积分价值(分) ≤ 毛利(分) × 该系数。默认 1.0（不超毛利）。
    'MARGIN_FACTOR': float(os.environ.get('POINT_MARGIN_FACTOR', '1.0') or 1.0),
    # 每人每日获取积分上限（积分，0=不限）；账户余额上限（积分，0=不限）。
    'DAILY_EARN_CAP': int(os.environ.get('POINT_DAILY_EARN_CAP', '0') or 0),
    'BALANCE_CAP': int(os.environ.get('POINT_BALANCE_CAP', '0') or 0),
    # 等级表：成长值(growth, 累计获得积分, 只增)驱动；rate=该等级消费返利率。
    # 首期 Now 只做「消费返 + 抵扣」，各等级统一用 CONSUME_RATE（等级倍率作 Next）。
    # 若需按等级差异化，把对应 rate 改成 >0 的具体值即可（0 表示沿用 CONSUME_RATE）。
    'TIERS': [
        {'level': 0, 'name': '新影迷',   'growth_min': 0,     'rate': 0},
        {'level': 1, 'name': '常客',     'growth_min': 500,   'rate': 0},
        {'level': 2, 'name': '资深影迷', 'growth_min': 2000,  'rate': 0},
        {'level': 3, 'name': '超级影迷', 'growth_min': 6000,  'rate': 0},
        {'level': 4, 'name': '骨灰影迷', 'growth_min': 15000, 'rate': 0},
    ],
}

# 分销（邀请返佣/归因/二级）合规停用开关：默认关。
# 关=停止计佣入账与归因写入（保留表/历史数据/代码，便于回滚）；开=恢复旧分销逻辑。
DISTRIBUTOR_ENABLED = os.environ.get('DISTRIBUTOR_ENABLED', 'false').lower() == 'true'

# JWT
JWT_EXPIRE_SECONDS = int(os.environ.get('JWT_EXPIRE_SECONDS', '7200'))
# 后台管理 token 有效期（独立于 C 端，建议短一些）
ADMIN_TOKEN_EXPIRE_SECONDS = int(os.environ.get('ADMIN_TOKEN_EXPIRE_SECONDS', '7200'))

# ===== CORS：仅对 B 端后台 /api/v1/admin 生效 =====
# 正式前端服务（admin-ui）与 API 同域部署（/ops/**），浏览器不发跨域请求，无需 CORS。
# 默认关闭「允许任意来源」；仅当过渡期仍用 file:// 打开 prototype/admin-rules.html 时，
# 设环境变量 ADMIN_CORS_ALLOW_ALL=true 临时放开（上线正式前端后应移除）。
# 如需白名单：ADMIN_CORS_ALLOWED_ORIGINS=https://ops.example.com,https://a.b.com
CORS_URLS_REGEX = r'^/api/v1/admin/.*$'
CORS_ALLOW_ALL_ORIGINS = os.environ.get('ADMIN_CORS_ALLOW_ALL', 'false').lower() == 'true'
CORS_ALLOW_CREDENTIALS = False
CORS_ALLOWED_ORIGIN_REGEXES = [
    r.strip() for r in os.environ.get('ADMIN_CORS_ALLOWED_ORIGINS', '').split(',') if r.strip()
]  # 正则白名单，如 ^https://[a-z0-9.-]+\.example\.com$
CORS_ALLOW_HEADERS = (
    'accept', 'accept-encoding', 'authorization', 'content-type', 'dnt',
    'origin', 'user-agent', 'x-csrftoken', 'x-requested-with',
    'x-task-token', 'x-trace-id',
)
CORS_ALLOW_METHODS = ('DELETE', 'GET', 'OPTIONS', 'PATCH', 'POST', 'PUT')
CORS_PREFLIGHT_MAX_AGE = 86400

# ===== 运维/定时任务 =====
# 内部同步 HTTP 接口的共享令牌（手动/外部触发用），请求头 X-Task-Token 携带。
# 留空则该 HTTP 接口一律拒绝执行（fail-closed，防止误暴露）。内置定时器不受此约束。
TASK_TOKEN = os.environ.get('TASK_TOKEN', '')
# 影片同步任务默认城市（麻花 cityId，成都=8）。可被接口 body 的 city 覆盖。
SYNC_DEFAULT_CITY = os.environ.get('SYNC_DEFAULT_CITY', '8')
# 内置定时器（APScheduler，进程内触发，不依赖外部调用）。默认开启；
# 微信云托管须保证服务「最小副本数 ≥ 1」，否则缩容到 0 时进程不在、定时器不触发。
ENABLE_SCHEDULER = os.environ.get('ENABLE_SCHEDULER', 'true').lower() == 'true'
# 每天同步的北京时间时点，逗号分隔 HH:MM。去重用进程内缓存锁（LocMem），建议单副本常驻。
# 注意：影片已改为按需实时拉取（pull_movies），此配置仅遗留，定时器不再使用。
SYNC_CRON_TIMES = os.environ.get('SYNC_CRON_TIMES', '00:30,13:00')
# 待映全量拉取定时任务间隔（分钟，默认 720 = 12 小时）。comingList 为全国分页数据，
# 定时拉取上限 100 条、循环拉到拉空为止；列表接口另有 12 小时缓存。
COMING_PULL_MINUTES = int(os.environ.get('COMING_PULL_MINUTES', '720'))

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

# ===== 运营上传图片（存 MySQL）体积上限 =====
# 单图 4MB；放宽请求体内存阈值以免 multipart 上传被判 DataTooBig。
# FILE_UPLOAD_MAX_MEMORY_SIZE 设为 4MB → 4MB 以内图片直接进内存，读取入库无需临时盘文件。
DATA_UPLOAD_MAX_MEMORY_SIZE = int(os.environ.get('DATA_UPLOAD_MAX_MEMORY_SIZE', 5 * 1024 * 1024))
FILE_UPLOAD_MAX_MEMORY_SIZE = int(os.environ.get('FILE_UPLOAD_MAX_MEMORY_SIZE', 4 * 1024 * 1024))

# ===== 运营后台前端（admin-ui 构建产物）同域托管 =====
# Docker 多阶段构建会把 admin-ui/dist 拷到 /app/ops_static（见 Dockerfile）；
# 本地未构建时目录不存在，/ops/ 自动不挂载（config/urls.py）。
# 自有云服务器改走 Nginx 托管时，设 OPS_UI_DIR 为空串即可关闭 Django 托管。
OPS_UI_DIR = os.environ.get('OPS_UI_DIR', str(BASE_DIR / 'ops_static'))
