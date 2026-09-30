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
