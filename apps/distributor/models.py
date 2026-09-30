"""分销模型：distributor / commission_rule / commission_record / wallet / wallet_txn / withdrawal / risk_event。"""
from django.db import models


class Distributor(models.Model):
    """分销关系（层级≤2）。"""
    user_id = models.BigIntegerField(unique=True, verbose_name='用户ID')
    invite_code = models.CharField(max_length=16, unique=True, verbose_name='推广码')
    parent_id = models.BigIntegerField(null=True, verbose_name='直属上级')
    grand_id = models.BigIntegerField(null=True, verbose_name='间接上级')
    level_path = models.CharField(max_length=255, verbose_name='物化路径')
    bind_at = models.DateTimeField(verbose_name='绑定时间')
    source = models.SmallIntegerField(verbose_name='1码 2小程序码 3口令')
    status = models.SmallIntegerField(default=1, verbose_name='状态')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'distributor'
        indexes = [
            models.Index(fields=['parent_id']),
            models.Index(fields=['level_path']),
        ]


class CommissionRule(models.Model):
    """佣金规则。"""
    SCOPE_GLOBAL = 1
    SCOPE_CINEMA = 2
    SCOPE_MOVIE = 3
    SCOPE_ACTIVITY = 4

    MODE_DIFF = 1       # 差价
    MODE_RATE = 2       # 比例

    scope = models.SmallIntegerField(verbose_name='1全局 2影院 3影片 4活动')
    scope_ref = models.CharField(max_length=64, null=True, verbose_name='范围引用')
    mode = models.SmallIntegerField(verbose_name='1差价 2比例')
    rate = models.DecimalField(max_digits=6, decimal_places=4, null=True, verbose_name='费率')
    fixed_diff = models.BigIntegerField(null=True, verbose_name='固定差价(分)')
    min_amt = models.BigIntegerField(default=0, verbose_name='最低佣金')
    max_amt = models.BigIntegerField(default=0, verbose_name='最高佣金')
    effective_from = models.DateTimeField(verbose_name='生效时间')
    effective_to = models.DateTimeField(null=True, verbose_name='失效时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'commission_rule'


class CommissionRecord(models.Model):
    """佣金流水。"""
    TYPE_INCOME = 1
    TYPE_DEDUCT = 2

    STATUS_PENDING = 1    # 在途
    STATUS_SETTLED = 2    # 已结算
    STATUS_INVALID = 3    # 已失效

    rec_no = models.CharField(max_length=64, unique=True, verbose_name='流水号')
    order_id = models.BigIntegerField(verbose_name='订单ID')
    buyer_id = models.BigIntegerField(verbose_name='下单人')
    promoter_id = models.BigIntegerField(verbose_name='得佣人')
    level = models.SmallIntegerField(verbose_name='1直属 2间接')
    base_amount = models.BigIntegerField(verbose_name='售价(分)')
    settle_amount = models.BigIntegerField(verbose_name='结算价(分)')
    commission_amount = models.BigIntegerField(verbose_name='佣金(分)')
    type = models.SmallIntegerField(verbose_name='1收入 2扣回')
    status = models.SmallIntegerField(verbose_name='1在途 2已结算 3已失效')
    settle_at = models.DateTimeField(null=True, verbose_name='结算时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'commission_record'
        unique_together = [('order_id', 'promoter_id', 'level', 'type')]
        indexes = [models.Index(fields=['promoter_id', 'status'])]


class Wallet(models.Model):
    """分销钱包。"""
    user_id = models.BigIntegerField(unique=True, verbose_name='用户ID')
    balance = models.BigIntegerField(default=0, verbose_name='可提余额(分)')
    frozen = models.BigIntegerField(default=0, verbose_name='提现冻结(分)')
    total_income = models.BigIntegerField(default=0, verbose_name='累计收入')
    total_withdraw = models.BigIntegerField(default=0, verbose_name='累计提现')
    version = models.IntegerField(default=0, verbose_name='CAS')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'wallet'


class WalletTxn(models.Model):
    """钱包流水（只增）。"""
    BIZ_SETTLE = 1       # 佣金结算
    BIZ_DEDUCT = 2       # 退款扣回
    BIZ_FREEZE = 3       # 提现冻结
    BIZ_WITHDRAW_OK = 4  # 提现成功
    BIZ_UNFREEZE = 5     # 提现驳回解冻

    wallet_id = models.BigIntegerField(verbose_name='钱包ID')
    biz_type = models.SmallIntegerField(verbose_name='业务类型')
    amount = models.BigIntegerField(verbose_name='带符号金额(分)')
    ref_no = models.CharField(max_length=64, null=True, verbose_name='关联单号')
    balance_after = models.BigIntegerField(verbose_name='变动后余额')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'wallet_txn'
        indexes = [models.Index(fields=['wallet_id', 'created_at'])]


class Withdrawal(models.Model):
    """提现单。"""
    STATUS_WAIT = 10      # 待审核
    STATUS_PAYING = 20    # 打款中
    STATUS_OK = 30        # 成功
    STATUS_FAIL = 40      # 失败
    STATUS_RETURNED = 50  # 已退回

    wd_no = models.CharField(max_length=64, unique=True, verbose_name='提现单号')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    amount = models.BigIntegerField(verbose_name='金额(分)')
    fee = models.BigIntegerField(default=0, verbose_name='手续费(分)')
    channel = models.SmallIntegerField(verbose_name='1微信零钱 2银行卡')
    account_ref = models.CharField(max_length=128, null=True, verbose_name='账户引用')
    realname_verified = models.SmallIntegerField(default=0, verbose_name='是否实名')
    status = models.SmallIntegerField(verbose_name='状态')
    pay_no = models.CharField(max_length=64, null=True, verbose_name='代付流水')
    callback_raw = models.JSONField(null=True, verbose_name='回调原文')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'withdrawal'
        indexes = [models.Index(fields=['user_id', 'status'])]


class RiskEvent(models.Model):
    """风控事件。"""
    user_id = models.BigIntegerField(verbose_name='用户ID')
    type = models.CharField(max_length=32, verbose_name='事件类型')
    score = models.IntegerField(default=0, verbose_name='评分')
    action = models.SmallIntegerField(verbose_name='1放行 2冻结佣金 3人工审')
    ref = models.CharField(max_length=64, null=True, verbose_name='引用')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'risk_event'
        indexes = [models.Index(fields=['user_id'])]
