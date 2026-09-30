"""公共表：幂等记录 / 对账差异 / 任务日志。"""
from django.db import models


class IdempotentRecord(models.Model):
    """通用幂等表。"""
    biz_type = models.CharField(max_length=32, verbose_name='业务类型')
    biz_key = models.CharField(max_length=128, verbose_name='幂等键')
    result_snapshot = models.JSONField(null=True, verbose_name='结果快照')
    expire_at = models.DateTimeField(verbose_name='过期时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'idempotent_record'
        unique_together = [('biz_type', 'biz_key')]


class ReconciliationDiff(models.Model):
    """对账差异。"""
    bill_date = models.DateField(verbose_name='对账日期')
    source = models.CharField(max_length=16, verbose_name='来源 MAHUA/PAY/LOCAL')
    order_ext_no = models.CharField(max_length=64, null=True, verbose_name='订单号')
    field = models.CharField(max_length=64, verbose_name='字段')
    local_val = models.CharField(max_length=128, null=True, verbose_name='本地值')
    remote_val = models.CharField(max_length=128, null=True, verbose_name='远端值')
    diff_type = models.CharField(max_length=32, verbose_name='差异类型')
    handle_status = models.SmallIntegerField(default=0, verbose_name='处理状态 0待处理 1已处理 2忽略')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'reconciliation_diff'
        indexes = [models.Index(fields=['bill_date', 'handle_status'])]


class JobRunLog(models.Model):
    """定时任务执行记录。"""
    job_name = models.CharField(max_length=64, verbose_name='任务名')
    shard = models.IntegerField(default=0, verbose_name='分片')
    biz_date = models.DateField(null=True, verbose_name='业务日期')
    status = models.SmallIntegerField(verbose_name='0运行中 1成功 2失败')
    rows_affected = models.IntegerField(default=0, verbose_name='影响行数')
    started_at = models.DateTimeField(verbose_name='开始时间')
    finished_at = models.DateTimeField(null=True, verbose_name='结束时间')

    class Meta:
        db_table = 'job_run_log'
        indexes = [models.Index(fields=['job_name', 'biz_date'])]
