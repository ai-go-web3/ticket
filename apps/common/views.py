from django.http import JsonResponse


def health(request):
    """健康检查。"""
    return JsonResponse({'code': 0, 'msg': 'ok', 'data': {'status': 'UP'}})
