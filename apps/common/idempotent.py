"""通用幂等工具。

基于 idempotent_record 表 + 数据库唯一键保证幂等。
"""
from django.db import IntegrityError
from django.utils import timezone


def idempotent_exec(biz_type, biz_key, fn, expire_seconds=86400):
    """幂等执行：同一 (biz_type, biz_key) 只执行一次 fn，重复调用返回之前的结果快照。

    Args:
        biz_type: 业务类型，如 'dispatch' / 'refund' / 'withdraw' / 'callback'
        biz_key: 幂等键
        fn: 无参回调，返回需要被记录的结果（可 JSON 序列化）
        expire_seconds: 幂等记录有效期

    Returns:
        (executed, result) —— executed=True 表示本次真实执行；False 表示命中已执行记录。
    """
    from apps.common.models import IdempotentRecord  # 延迟导入避免循环

    # 1. 尝试插入占位记录（唯一键兜底）
    try:
        IdempotentRecord.objects.create(
            biz_type=biz_type,
            biz_key=biz_key,
            result_snapshot=None,
            expire_at=timezone.now() + timezone.timedelta(seconds=expire_seconds),
        )
    except IntegrityError:
        # 已存在 -> 返回之前结果
        rec = IdempotentRecord.objects.get(biz_type=biz_type, biz_key=biz_key)
        return False, rec.result_snapshot

    # 2. 首次执行
    result = fn()
    IdempotentRecord.objects.filter(biz_type=biz_type, biz_key=biz_key).update(
        result_snapshot=result,
    )
    return True, result
