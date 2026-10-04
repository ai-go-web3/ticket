"""订单相关模型：ticket_order / mahua_dispatch / order_payment / ticket。"""
from django.db import models


class TicketOrder(models.Model):
    """订单主表（我方权威）。"""
    STATUS_PAYING = 10      # 待支付
    STATUS_DISPATCHING = 20 # 出票中
    STATUS_WAIT_PICK = 30   # 待取票
    STATUS_DONE = 40        # 已完成(确认收货)
    STATUS_CLOSED = 50      # 已关闭
    STATUS_DISPATCH_FAIL = 60  # 出票失败
    STATUS_REFUNDING = 70   # 退款中
    STATUS_REFUNDED = 80    # 已退款
    STATUS_DISPUTE = 90     # 纠纷中

    PAY_NOT = 0
    PAY_DONE = 1
    PAY_REFUNDED = 2

    order_ext_no = models.CharField(max_length=64, unique=True, verbose_name='幂等订单号')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    schedule_id = models.BigIntegerField(verbose_name='排片ID')
    cinema_id = models.BigIntegerField(verbose_name='影院ID')
    movie_id = models.BigIntegerField(verbose_name='影片ID')
    up_schedule_id = models.CharField(max_length=64, verbose_name='麻花排片ID快照')
    seats_json = models.JSONField(verbose_name='座位快照')
    seat_count = models.SmallIntegerField(verbose_name='座位数')
    ticket_amount = models.BigIntegerField(verbose_name='票面总额(分)')
    service_fee = models.BigIntegerField(default=0, verbose_name='服务费(分)')
    discount_amount = models.BigIntegerField(default=0, verbose_name='优惠(分)')
    pay_amount = models.BigIntegerField(verbose_name='实付(分)')
    settle_amount = models.BigIntegerField(null=True, verbose_name='结算价(分)')
    # 财务快照：建单时的成本上浮费率与预估成本（Σ原始fastPrice，分）。
    # 实际成本以 settle_amount（麻花 confirmPrice）为准；est_cost 用于选座时点预估与对账差异分析。
    price_rate = models.FloatField(null=True, verbose_name='成本上浮费率快照')
    est_cost_amount = models.BigIntegerField(null=True, verbose_name='预估成本(分,Σ原始fastPrice)')
    # 购票模式：tehui=特惠（放单不传 model，麻花默认 0）；kuai=快速（放单 model=1 快速通道）
    buy_mode = models.CharField(max_length=8, default='tehui', verbose_name='购票模式')
    mobile = models.CharField(max_length=20, verbose_name='取票手机')
    status = models.SmallIntegerField(verbose_name='状态')
    pay_status = models.SmallIntegerField(default=0, verbose_name='0未付 1已付 2已退')
    promoter_id = models.BigIntegerField(null=True, verbose_name='推广人')
    confirmed_at = models.DateTimeField(null=True, verbose_name='确认收货时间')
    close_reason = models.CharField(max_length=64, null=True, verbose_name='关闭原因')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ticket_order'
        indexes = [
            models.Index(fields=['user_id', 'status']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['promoter_id']),
        ]


class MahuaDispatch(models.Model):
    """麻花放单映射/状态。"""
    STATUS_DISPATCHED = 0   # 已放单
    STATUS_TICKETING = 1    # 出票中
    STATUS_TICKETED = 2     # 已出票
    STATUS_FAIL = 3         # 出票失败
    STATUS_INTERCEPTED = 4  # 已拦截
    STATUS_REFUNDED = 5     # 已退款
    STATUS_PENDING = 6      # 待放单/待补偿（提交超时或返回异常，用查询接口补偿）

    order_ext_no = models.CharField(max_length=64, unique=True, verbose_name='订单号')
    mahua_order_no = models.CharField(max_length=64, null=True, verbose_name='麻花放单号')
    request_body = models.JSONField(verbose_name='请求体')
    response_body = models.JSONField(null=True, verbose_name='响应体')
    dispatch_status = models.SmallIntegerField(default=0, verbose_name='放单状态')
    ticket_code = models.CharField(max_length=128, null=True, verbose_name='取票码')
    barcode = models.CharField(max_length=255, null=True, verbose_name='二维码')
    last_query_at = models.DateTimeField(null=True, verbose_name='最近查询时间')
    retry_count = models.IntegerField(default=0, verbose_name='重试次数')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'mahua_dispatch'
        indexes = [models.Index(fields=['mahua_order_no'])]


class OrderPayment(models.Model):
    """支付流水。"""
    STATUS_WAIT = 0
    STATUS_SUCCESS = 1
    STATUS_FAIL = 2
    STATUS_REFUNDED = 3

    order_id = models.BigIntegerField(verbose_name='订单ID')
    pay_no = models.CharField(max_length=64, unique=True, verbose_name='支付流水号')
    channel = models.CharField(max_length=16, default='WX', verbose_name='渠道')
    amount = models.BigIntegerField(verbose_name='金额(分)')
    status = models.SmallIntegerField(verbose_name='0待付 1成功 2失败 3已退')
    callback_raw = models.JSONField(null=True, verbose_name='回调原文')
    paid_at = models.DateTimeField(null=True, verbose_name='支付时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'order_payment'
        indexes = [models.Index(fields=['order_id'])]


class Ticket(models.Model):
    """票券（一座位一票）。"""
    STATUS_WAIT_PICK = 1
    STATUS_PICKED = 2
    STATUS_WATCHED = 3
    STATUS_REFUNDED = 4

    order_id = models.BigIntegerField(verbose_name='订单ID')
    seat_no = models.CharField(max_length=32, verbose_name='座位号')
    ticket_code = models.CharField(max_length=128, null=True, verbose_name='取票码')
    barcode = models.CharField(max_length=255, null=True, verbose_name='二维码')
    status = models.SmallIntegerField(default=1, verbose_name='1待取 2已取 3已观影 4已退')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'ticket'
        indexes = [models.Index(fields=['order_id'])]
