"""中间件：traceId + 请求日志 + 全局异常兜底。"""
import logging
import uuid

from django.http import JsonResponse

logger = logging.getLogger('app')


class RequestTraceMiddleware:
    """为每个请求注入 traceId，便于链路追踪与日志关联。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        trace_id = request.headers.get('X-Trace-Id') or uuid.uuid4().hex[:16]
        request.trace_id = trace_id
        response = self.get_response(request)
        response['X-Trace-Id'] = trace_id
        return response


class GlobalExceptionMiddleware:
    """最外层异常兜底（DRF 未捕获时返回统一 JSON，避免 500 白屏）。"""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            return self.get_response(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception('global exception: %s', exc)
            return JsonResponse(
                {'code': 50000, 'msg': '服务器内部错误', 'data': None},
                status=500,
            )


# 需要登录态的接口前缀（其余如 catalog / wx-login / 各类回调 / sync-movies / health 均公开）
PROTECTED_PREFIXES = (
    '/api/v1/order',
    '/api/v1/seat',
    '/api/v1/distributor',
    '/api/v1/refund',
    '/api/v1/pay/unified',
    '/api/v1/pay/mock',
    '/api/v1/auth/profile',
    '/api/v1/auth/update-profile',
    '/api/v1/auth/bind-phone',
)
# 受保护前缀中的豁免路径（运维接口，走 TASK_TOKEN 自鉴权）
PROTECTED_EXEMPT_PATHS = ('/api/v1/order/finance/daily',)
# B 端后台前缀：整体绕过 C 端强制 401，由 apps.adminapi 的 IsAdmin 权限自守
ADMIN_EXEMPT_PREFIX = '/api/v1/admin'


class JWTAuthMiddleware:
    """统一登录态校验：受保护接口无有效 token 时返回 401（触发前端静默重登），
    有效时把 user_id 挂到 request 上供视图使用。公开接口一律放行。"""

    def __init__(self, get_response):
        self.get_response = get_response

    @staticmethod
    def _resolve_user_id(request):
        """从 Authorization: Bearer <token> 解析 user_id，失败返回 None。"""
        auth = request.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return None
        from apps.auths.authentication import decode_token
        from apps.auths.models import AppUser
        try:
            payload = decode_token(auth[7:].strip())
            return AppUser.objects.filter(id=payload.get('uid'), deleted=0).values_list('id', flat=True).first()
        except Exception:  # noqa: BLE001
            return None

    def __call__(self, request):
        path = request.path
        uid = self._resolve_user_id(request)
        if uid is not None:
            request.user_id = uid  # 公开接口若带有效 token 也顺带注入

        # B 端后台整体绕过 C 端强制 401（管理 token 的 aid 不在 app_user 表，
        # 若走下面的校验会误判未登录）；后台接口由 apps.adminapi.IsAdmin 自守。
        if path.startswith(ADMIN_EXEMPT_PREFIX):
            return self.get_response(request)

        protected = any(path.startswith(p) for p in PROTECTED_PREFIXES)
        protected = protected and not any(path.startswith(e) for e in PROTECTED_EXEMPT_PATHS)
        if protected and request.method != 'OPTIONS' and uid is None:
            return JsonResponse(
                {'code': 40100, 'msg': '未登录或登录已失效', 'data': None},
                status=401,
            )
        return self.get_response(request)
