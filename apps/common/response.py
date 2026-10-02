"""统一响应与业务异常。

统一响应格式：{ code, msg, data, traceId }
code == 0 表示成功；非 0 为业务错误码。
"""
import uuid

from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler


class BizError(Exception):
    """业务异常：message 面向用户，code 为业务错误码。"""

    def __init__(self, message, code=40000, http_status=status.HTTP_200_OK):
        self.message = message
        self.code = code
        self.http_status = http_status
        super().__init__(message)


class ErrorCode:
    """业务错误码定义。"""
    OK = 0
    # 通用
    PARAM_ERROR = 40001
    UNAUTHORIZED = 40100
    FORBIDDEN = 40300
    NOT_FOUND = 40400
    PHONE_BIND_FAILED = 40010     # 手机号获取/绑定失败（非登录失效，勿用 40100，以免前端误清 token 重登）
    # 交易
    SEAT_TAKEN = 41001            # 座位已被抢占
    SEAT_LOCK_EXPIRED = 41002     # 锁座已过期
    ORDER_STATE_ILLEGAL = 42001   # 非法状态迁移
    ORDER_NOT_EXIST = 42002
    PAY_NOT_DONE = 43001
    # 退款
    REFUND_NOT_ALLOWED = 44001
    INTERCEPT_FAILED = 44002      # 麻花拦截失败，转人工
    # 分销
    WITHDRAW_INSUFFICIENT = 45001
    WITHDRAW_NOT_VERIFIED = 45002
    # 上游
    UP_ERROR = 50000
    UP_RATE_LIMIT = 50001


def ok(data=None, msg='ok'):
    """成功响应。"""
    return Response({'code': ErrorCode.OK, 'msg': msg, 'data': data})


def fail(message, code=40000, http_status=status.HTTP_200_OK):
    """失败响应。"""
    return Response(
        {'code': code, 'msg': message, 'data': None},
        status=http_status,
    )


def _get_trace_id(request):
    return getattr(request, 'trace_id', '') or uuid.uuid4().hex[:16]


def api_exception_handler(exc, context):
    """DRF 统一异常处理器。

    业务异常(BizError) -> 友好提示；
    其他 DRF 校验/权限异常 -> 转统一格式；
    未知异常 -> 记录并返回 500。
    """
    request = context.get('request')
    trace_id = _get_trace_id(request) if request else ''

    # 鉴权失败（未登录 / token 无效）统一 40100 + HTTP 401
    from rest_framework.exceptions import NotAuthenticated, AuthenticationFailed
    if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
        return Response(
            {'code': ErrorCode.UNAUTHORIZED, 'msg': '未登录或登录已失效',
             'data': None, 'traceId': trace_id},
            status=status.HTTP_401_UNAUTHORIZED,
        )

    # 业务异常
    if isinstance(exc, BizError):
        return Response(
            {'code': exc.code, 'msg': exc.message, 'data': None, 'traceId': trace_id},
            status=exc.http_status,
        )

    # DRF 默认处理（校验错误、权限、404 等）
    response = drf_exception_handler(exc, context)
    if response is not None:
        data = response.data
        if isinstance(data, dict):
            # 取第一个错误信息做简要提示
            msg = _flatten_error(data)
        else:
            msg = str(data)
        return Response(
            {'code': _map_status_to_code(response.status_code),
             'msg': msg, 'data': None, 'traceId': trace_id},
            status=response.status_code,
        )

    # 未知异常
    import logging
    logger = logging.getLogger('app')
    logger.exception('unhandled exception: %s', exc)
    return Response(
        {'code': 50000, 'msg': '服务器内部错误', 'data': None, 'traceId': trace_id},
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def _flatten_error(data):
    if isinstance(data, dict):
        for v in data.values():
            r = _flatten_error(v)
            if r:
                return r
    elif isinstance(data, list):
        for v in data:
            r = _flatten_error(v)
            if r:
                return r
    else:
        return str(data)
    return ''


def _map_status_to_code(http_status):
    return {
        status.HTTP_400_BAD_REQUEST: ErrorCode.PARAM_ERROR,
        status.HTTP_401_UNAUTHORIZED: ErrorCode.UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN: ErrorCode.FORBIDDEN,
        status.HTTP_404_NOT_FOUND: ErrorCode.NOT_FOUND,
    }.get(http_status, 40000)
