"""退款相关模型：refund / dispute / dispute_msg。"""
from django.db import models


class Refund(models.Model):
    """退款单（未出票=拦截；已出票=纠纷驱动）。"""
    TYPE_INTERCEPT = 1      # 拦截取消
    TYPE_DISPUTE = 2        # 个人原因退票(纠纷)
    TYPE_DISPATCH_FAIL = 3  # 出票失败自动退
    TYPE_PAY_AFTER_CLOSE = 4  # 关单后支付到账（关单/支付竞态）自动原路退
    TYPE_UP_REFUND = 5        # 麻花退票回调(ticketRefund)后对用户原路退

    STATUS_ACCEPTED = 10       # 受理
    STATUS_INTERCEPTING = 20   # 拦截中
    STATUS_WAIT_MAHUA = 30     # 待麻花确认
    STATUS_REFUNDING = 40      # 退款中
    STATUS_ARRIVED = 50        # 已到账
    STATUS_FAIL = 60           # 失败/拦截失败转人工

    refund_ext_no = models.CharField(max_length=64, unique=True, verbose_name='退款幂等单号')
    order_id = models.BigIntegerField(verbose_name='订单ID')
    type = models.SmallIntegerField(verbose_name='1拦截 2纠纷退票 3出票失败自动退')
    reason = models.CharField(max_length=64, null=True, verbose_name='原因')
    fee = models.BigIntegerField(default=0, verbose_name='手续费(分)')
    refund_amount = models.BigIntegerField(verbose_name='退回金额(分)')
    mahua_refund_no = models.CharField(max_length=64, null=True, verbose_name='麻花退款号')
    wx_refund_no = models.CharField(max_length=64, null=True, verbose_name='微信退款号')
    status = models.SmallIntegerField(verbose_name='状态')
    retry_count = models.IntegerField(default=0, verbose_name='微信退款重试次数')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'refund'
        indexes = [
            models.Index(fields=['order_id']),
            models.Index(fields=['status']),
        ]


class Dispute(models.Model):
    """纠纷单。"""
    STATUS_STARTED = 10    # 已发起
    STATUS_PROCESSING = 20 # 处理中
    STATUS_AGREED = 30     # 已同意
    STATUS_REJECTED = 40   # 已拒绝
    STATUS_CANCELLED = 50  # 已取消
    STATUS_DONE = 60       # 已完结

    order_id = models.BigIntegerField(verbose_name='订单ID')
    refund_id = models.BigIntegerField(null=True, verbose_name='退款单ID')
    up_dispute_no = models.CharField(max_length=64, null=True, verbose_name='麻花纠纷单号')
    reason_code = models.CharField(max_length=32, verbose_name='纠纷原因码')
    reason_text = models.CharField(max_length=128, null=True, verbose_name='原因文本')
    status = models.SmallIntegerField(verbose_name='状态')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'dispute'
        indexes = [
            models.Index(fields=['order_id']),
            models.Index(fields=['up_dispute_no']),
        ]


class DisputeMsg(models.Model):
    """纠纷往来消息。"""
    ROLE_USER = 1
    ROLE_PLATFORM = 2
    ROLE_MAHUA = 3

    dispute_id = models.BigIntegerField(verbose_name='纠纷ID')
    from_role = models.SmallIntegerField(verbose_name='1用户 2平台 3麻花客服')
    content = models.CharField(max_length=1024, verbose_name='内容')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'dispute_msg'
        indexes = [models.Index(fields=['dispute_id'])]
