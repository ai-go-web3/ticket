"""订阅消息通知模型：发送任务 + 授权额度。

四类事件共用一张 NotifyTask 表：
- 事件即时型（出票成功 issued / 退款成功 refunded）：due_at=入队时刻，dispatcher 下一轮即发；
- 到点定时型（支付倒计时 pay_remind / 开场前取票 pickup_remind）：due_at=算好的提醒时刻，
  到点后 dispatcher 复检订单状态再决定发送或跳过。

设计要点见 docs/消息通知技术方案.md。发送走微信一次性订阅消息：一次授权=一条额度，
故额度采集（SubscriptionQuota）与发送扣减分离，额度不足/未配模板一律视为「发不出」的
常态（NO_QUOTA 终态，不重试），而非异常。
"""
from django.db import models


class NotifyTask(models.Model):
    """订阅消息发送任务（幂等：同一订单同一事件仅一条）。"""
    EVENT_PAY_REMIND = 'pay_remind'        # 支付倒计时提醒（催付）
    EVENT_PICKUP_REMIND = 'pickup_remind'  # 开场前取票提醒（催取票）
    EVENT_ISSUED = 'issued'                # 出票成功
    EVENT_REFUNDED = 'refunded'            # 退款成功
    EVENTS = (
        (EVENT_PAY_REMIND, '支付倒计时提醒'),
        (EVENT_PICKUP_REMIND, '开场前取票提醒'),
        (EVENT_ISSUED, '出票成功'),
        (EVENT_REFUNDED, '退款成功'),
    )

    STATUS_PENDING = 0     # 待发（due_at 到点后由 dispatcher 领取发送）
    STATUS_SUCCESS = 1     # 发送成功
    STATUS_RETRY = 2       # 发送失败待重试（网络/微信 5xx，退避后重试）
    STATUS_FAILED = 3      # 失败终态（重试超限 / openid 配置错误）
    STATUS_NO_QUOTA = 4    # 发不出终态（用户未授权额度，或模板未配置）
    STATUS_SKIPPED = 5     # 复检后跳过（订单状态已变，如已付/已取/已开场，无需提醒）
    STATUS_PROCESSING = 6  # 领取中（防并发的临时占位，崩溃残留由 dispatcher 超时回收）
    STATUSES = (
        (STATUS_PENDING, '待发'),
        (STATUS_SUCCESS, '成功'),
        (STATUS_RETRY, '待重试'),
        (STATUS_FAILED, '失败'),
        (STATUS_NO_QUOTA, '无额度/未配模板'),
        (STATUS_SKIPPED, '跳过'),
        (STATUS_PROCESSING, '处理中'),
    )

    event = models.CharField(max_length=20, choices=EVENTS, verbose_name='事件类型')
    order_id = models.BigIntegerField(verbose_name='订单ID')
    user_id = models.BigIntegerField(verbose_name='用户ID(AppUser.id)')
    template_id = models.CharField(max_length=64, blank=True, verbose_name='模板ID快照(发送时以配置为准)')
    due_at = models.DateTimeField(verbose_name='计划发送时刻')
    payload = models.JSONField(default=dict, blank=True, verbose_name='附加数据(如退款金额/原因)')
    status = models.SmallIntegerField(default=STATUS_PENDING, choices=STATUSES, verbose_name='状态')
    retry_count = models.SmallIntegerField(default=0, verbose_name='重试次数')
    err_msg = models.CharField(max_length=255, blank=True, verbose_name='最近失败/跳过原因')
    sent_at = models.DateTimeField(null=True, verbose_name='实际发送成功时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'notify_task'
        constraints = [
            # 幂等去重：同一订单同一事件只建一条，防回调重推 / 定时器重复触发
            models.UniqueConstraint(fields=['event', 'order_id'], name='uniq_notify_event_order'),
        ]
        indexes = [
            models.Index(fields=['status', 'due_at'], name='idx_notify_status_due'),
        ]

    def __str__(self):
        return f'{self.event}-{self.order_id}-{self.get_status_display()}'


class SubscriptionQuota(models.Model):
    """一次性订阅消息授权额度：用户每授权一个模板 +1，每发送一条 -1。"""
    user_id = models.BigIntegerField(verbose_name='用户ID')
    template_id = models.CharField(max_length=64, verbose_name='微信模板ID')
    count = models.IntegerField(default=0, verbose_name='剩余额度')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'subscription_quota'
        constraints = [
            models.UniqueConstraint(fields=['user_id', 'template_id'], name='uniq_quota_user_template'),
        ]

    def __str__(self):
        return f'{self.user_id}-{self.template_id}={self.count}'
