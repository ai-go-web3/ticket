"""积分/分销模型：wallet(积分账户) / wallet_txn(积分流水) / deduct_rule(抵扣规则)
  + 遗留分销表 distributor / commission_rule / commission_record / withdrawal / risk_event。

现行方向 = 消费积分体系：积分以「自身消费/行为」入账（earn_consume 等），只能抵扣电影票，
不可提现/转让/折现，获取与「是否邀请到人」无关。
分销那套（Distributor 归因 / CommissionRecord 返佣 / CommissionRule 计佣 / Withdrawal 提现）
属旧合规风险方案，「停用但保留」：表与历史数据留存、代码短路不写入，便于回滚。
"""
from decimal import Decimal

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


class DeductRule(models.Model):
    """积分抵扣规则（消费侧策略；积分抵扣票款的比例/下限/上限/兑换率/有效期）。"""
    SCOPE_GLOBAL = 1
    SCOPE_ACTIVITY = 2

    name = models.CharField(max_length=64, verbose_name='规则名')
    scope = models.SmallIntegerField(default=1, verbose_name='1全局 2活动')
    scope_ref = models.CharField(max_length=64, null=True, blank=True, verbose_name='活动引用')
    fen_per_point = models.DecimalField(
        max_digits=6, decimal_places=4, default=Decimal('1.0000'),
        verbose_name='兑换率(1积分抵扣多少分)',
    )
    max_deduct_ratio = models.DecimalField(
        max_digits=5, decimal_places=4, default=Decimal('0.5000'),
        verbose_name='单笔最高抵扣比例',
    )
    min_self_pay = models.BigIntegerField(default=100, verbose_name='最低自付(分)')
    per_order_cap = models.BigIntegerField(default=0, verbose_name='单笔抵扣上限(分,0=不限)')
    daily_deduct_cap = models.BigIntegerField(default=0, verbose_name='每人每日抵扣上限(分,0=不限)')
    expire_days = models.IntegerField(default=365, verbose_name='积分有效期天数(0=不过期)')
    allow_grand = models.SmallIntegerField(default=0, verbose_name='【已废弃】二级分佣开关')
    stack_with_coupon = models.SmallIntegerField(default=0, verbose_name='可与优惠券叠加(0二选一)')
    is_active = models.SmallIntegerField(default=1, verbose_name='启用')
    effective_from = models.DateTimeField(null=True, blank=True, verbose_name='生效时间')
    effective_to = models.DateTimeField(null=True, blank=True, verbose_name='失效时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'deduct_rule'
        indexes = [models.Index(fields=['scope', 'is_active'])]


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
    """积分账户（消费积分体系：balance=可用积分，只能抵扣电影票，不可提现/转让/折现）。

    单位：balance/frozen/total_income 均为「积分」（兑换率见 DeductRule.fen_per_point）。
    """
    user_id = models.BigIntegerField(unique=True, verbose_name='用户ID')
    balance = models.BigIntegerField(default=0, verbose_name='可用积分')
    frozen = models.BigIntegerField(default=0, verbose_name='下单抵扣冻结(积分)')
    total_income = models.BigIntegerField(default=0, verbose_name='累计获得积分')
    total_withdraw = models.BigIntegerField(default=0, verbose_name='累计提现(历史遗留·停用)')
    growth = models.BigIntegerField(default=0, verbose_name='成长值(累计获得积分·只增·驱动等级)')
    tier = models.SmallIntegerField(default=0, verbose_name='会员等级(0~4)')
    last_earn_date = models.DateField(null=True, blank=True, verbose_name='上次签到/入账日')
    streak = models.IntegerField(default=0, verbose_name='连续签到天数')
    expire_at = models.DateTimeField(null=True, blank=True, verbose_name='滚动过期锚点')
    version = models.IntegerField(default=0, verbose_name='CAS')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'wallet'


class WalletTxn(models.Model):
    """积分流水（只增）。amount 单位：积分（带符号）。"""
    BIZ_SETTLE = 1        # 获取入账（消费返/签到/任务/生日/直发…，用 scene 细分）
    BIZ_DEDUCT = 2        # 退款回冲（订单退款把已抵扣的积分退回余额）
    BIZ_FREEZE = 3        # 下单抵扣冻结
    BIZ_WITHDRAW_OK = 4   # 【遗留】提现成功，积分体系不再写入
    BIZ_UNFREEZE = 5      # 抵扣解冻（支付失败/取消）
    BIZ_USE_OK = 6        # 抵扣核销（支付成功后正式扣减冻结）
    BIZ_EXPIRE = 7        # 积分过期作废

    # 获取子类（仅 BIZ_SETTLE 使用；与「是否邀请到人」无关，全部挂自身行为）
    SCENE_CONSUME = 1     # 消费返积分
    SCENE_CHECKIN = 2     # 每日签到
    SCENE_TASK = 3        # 新手/任务
    SCENE_BIRTHDAY = 4    # 生日/会员日
    SCENE_ADMIN = 5       # 运营后台直发（补偿/活动）
    SCENE_EXCHANGE = 6    # 兑换（预留：积分商城出账）

    wallet_id = models.BigIntegerField(verbose_name='账户ID')
    biz_type = models.SmallIntegerField(verbose_name='业务类型')
    scene = models.SmallIntegerField(null=True, blank=True, verbose_name='获取子类(仅入账用)')
    remark = models.CharField(max_length=64, null=True, blank=True, verbose_name='展示文案')
    amount = models.BigIntegerField(verbose_name='带符号金额(积分)')
    ref_no = models.CharField(max_length=64, null=True, verbose_name='关联单号')
    balance_after = models.BigIntegerField(verbose_name='变动后余额(积分)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'wallet_txn'
        indexes = [models.Index(fields=['wallet_id', 'created_at'])]


class Withdrawal(models.Model):
    """提现单【遗留】：抵扣化改造后提现已停用，本表仅保留历史数据供对账/留痕，不再新增。"""
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
