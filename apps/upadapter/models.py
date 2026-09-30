"""麻花对接镜像模型：账户 / 充值 / 回调日志。"""
from django.db import models


class MahuaAccount(models.Model):
    """麻花账户余额镜像。"""
    balance = models.BigIntegerField(default=0, verbose_name='可用余额(分)')
    frozen = models.BigIntegerField(default=0, verbose_name='冻结(分)')
    total_recharge = models.BigIntegerField(default=0, verbose_name='累计充值')
    total_dispatch = models.BigIntegerField(default=0, verbose_name='累计放单(分)')
    snapshot_at = models.DateTimeField(verbose_name='快照时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'mahua_account'


class MahuaRecharge(models.Model):
    """麻花充值流水。"""
    out_trade_no = models.CharField(max_length=64, unique=True, verbose_name='我方充值单号')
    mahua_order_no = models.CharField(max_length=64, null=True, verbose_name='麻花充值单号')
    amount = models.BigIntegerField(verbose_name='金额(分)')
    channel = models.CharField(max_length=32, null=True, verbose_name='渠道')
    status = models.SmallIntegerField(default=0, verbose_name='0处理中 1成功 2失败')
    paid_at = models.DateTimeField(null=True, verbose_name='支付时间')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'mahua_recharge'


class MahuaCallbackLog(models.Model):
    """麻花回调日志（幂等 + 排障 + 对账）。"""
    biz_type = models.CharField(max_length=32, verbose_name='order/dispute/movie/schedule')
    dedup_key = models.CharField(max_length=128, unique=True, verbose_name='幂等键')
    payload = models.JSONField(verbose_name='报文')
    handle_status = models.SmallIntegerField(default=0, verbose_name='0待处理 1成功 2失败 3重复忽略')
    error_msg = models.CharField(max_length=512, null=True, verbose_name='错误信息')
    received_at = models.DateTimeField(auto_now_add=True, verbose_name='接收时间')

    class Meta:
        db_table = 'mahua_callback_log'
        indexes = [models.Index(fields=['biz_type', 'received_at'])]
