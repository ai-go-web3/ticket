"""座位锁模型：seat_lock / seat_lock_item。"""
from django.db import models


class SeatLock(models.Model):
    """我方本地座位锁（并发控制，非麻花锁）。"""
    STATUS_HOLD = 1        # 占用中
    STATUS_CONSUMED = 2    # 已下单
    STATUS_RELEASED = 3    # 已释放
    STATUS_EXPIRED = 4     # 已过期

    lock_token = models.CharField(max_length=36, unique=True, verbose_name='锁token')
    schedule_id = models.BigIntegerField(verbose_name='排片ID')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    seats_json = models.JSONField(verbose_name='座位快照')
    seat_count = models.SmallIntegerField(verbose_name='座位数')
    status = models.SmallIntegerField(verbose_name='1占用 2已下单 3已释放 4已过期')
    expire_at = models.DateTimeField(verbose_name='过期时间')
    order_id = models.BigIntegerField(null=True, verbose_name='订单ID')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'seat_lock'
        indexes = [
            models.Index(fields=['schedule_id', 'status']),
            models.Index(fields=['expire_at']),
        ]


class SeatLockItem(models.Model):
    """座位锁明细（唯一键防超卖）。"""
    lock_token = models.CharField(max_length=36, verbose_name='锁token')
    schedule_id = models.BigIntegerField(verbose_name='排片ID')
    seat_no = models.CharField(max_length=32, verbose_name='座位号 如7排8座')

    class Meta:
        db_table = 'seat_lock_item'
        unique_together = [('schedule_id', 'seat_no')]
        indexes = [models.Index(fields=['lock_token'])]
