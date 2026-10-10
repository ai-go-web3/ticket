"""积分商城模型。

三张核心表：
- MallItem      商城商品（可兑换的「电影票相关权益」），B 端运营配置，C 端浏览/兑换。
- MallRedemption 兑换订单（出账单），一次兑换一行，request_id 唯一保证幂等。
- Voucher       券码（兑换成功派生），带 30 天有效期，进「我的券包」，前台/下单核销用。

积分账务本身（余额、流水）仍归 apps/distributor 的 Wallet / WalletTxn，
本模块只负责「扣多少积分、出什么券、能不能出」，出账走 WalletTxn(scene=SCENE_EXCHANGE)。
"""
from django.db import models


class MallItem(models.Model):
    """商城商品（权益）。"""
    # 品类（严格限定「电影票相关权益」，不含实物 / 第三方礼品卡 / 平台特权）
    CAT_VOUCHER = 1        # 观影代金券
    CAT_FREE_TICKET = 2    # 免费观影票
    CAT_SNACK = 3          # 小食套餐券
    CAT_CHOICES = [
        (CAT_VOUCHER, '观影代金券'),
        (CAT_FREE_TICKET, '免费观影票'),
        (CAT_SNACK, '小食套餐券'),
    ]
    CAT_TEXT = dict(CAT_CHOICES)

    # 状态
    STATUS_OFF = 0         # 下架（C 端不展示 / 兑换报 ITEM_OFFLINE）
    STATUS_ON = 1          # 上架
    STATUS_CHOICES = [(STATUS_OFF, '下架'), (STATUS_ON, '上架')]

    # 展示角标
    BADGE_NONE = ''
    BADGE_HOT = 'HOT'
    BADGE_NEW = 'NEW'

    name = models.CharField(max_length=64, verbose_name='权益名称')
    category = models.SmallIntegerField(choices=CAT_CHOICES, default=CAT_VOUCHER, verbose_name='品类')
    cover_url = models.CharField(max_length=512, blank=True, default='', verbose_name='头图URL(空则前端回退示意图)')

    points_price = models.BigIntegerField(verbose_name='兑换所需积分')
    origin_price_fen = models.BigIntegerField(default=0, verbose_name='原价对照(分,0=无对照)')

    # 代金券抵扣属性（仅 CAT_VOUCHER 有意义；兑换时快照进 Voucher，运营改此值不影响已发出的券）
    face_value_fen = models.BigIntegerField(default=0, verbose_name='券面额(分,抵扣额)')
    use_threshold_fen = models.BigIntegerField(default=0, verbose_name='使用门槛(分,票面应付满此额可用,0=无门槛)')

    # 库存：stock_total=上架总份数，stock=剩余可兑；进度条/「仅剩X份」由二者派生
    stock_total = models.IntegerField(default=0, verbose_name='上架总库存(份)')
    stock = models.IntegerField(default=0, verbose_name='剩余库存(份)')

    daily_limit = models.IntegerField(default=3, verbose_name='每人每日同品类上限(张,0=不限)')
    expire_days = models.IntegerField(default=30, verbose_name='券有效期(天)')

    badge = models.CharField(max_length=8, blank=True, default='', verbose_name='角标 HOT/NEW/空')
    description = models.TextField(blank=True, default='', verbose_name='权益说明')
    notes = models.TextField(blank=True, default='', verbose_name='兑换须知')
    applicable = models.TextField(blank=True, default='', verbose_name='适用影院/范围说明')

    status = models.SmallIntegerField(choices=STATUS_CHOICES, default=STATUS_OFF, verbose_name='上下架')
    sort = models.IntegerField(default=0, verbose_name='排序(小者前)')

    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'mall_item'
        ordering = ['sort', '-id']
        indexes = [
            models.Index(fields=['status', 'deleted', 'sort']),
            models.Index(fields=['category']),
        ]

    def __str__(self):
        return f'{self.id}-{self.name}'

    @property
    def sold(self):
        return max(self.stock_total - self.stock, 0)

    @property
    def stock_status(self):
        """库存三态（走查 M7）：normal(>20%) / low(5%~20%) / out(=0)。"""
        if self.stock <= 0:
            return 'out'
        if self.stock_total > 0 and self.stock / self.stock_total <= 0.2:
            return 'low'
        return 'normal'


class MallRedemption(models.Model):
    """兑换订单（出账单）。"""
    STATUS_SUCCESS = 1     # 兑换成功（不可撤销，故无失败态落库；失败直接抛 BizError 回滚）

    redemption_no = models.CharField(max_length=64, unique=True, verbose_name='兑换单号')
    request_id = models.CharField(max_length=64, unique=True, verbose_name='幂等请求ID(客户端UUID)')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    item_id = models.BigIntegerField(verbose_name='商品ID')
    category = models.SmallIntegerField(verbose_name='品类快照')
    item_name = models.CharField(max_length=64, verbose_name='商品名快照')
    points_price = models.BigIntegerField(verbose_name='扣减积分快照')
    quantity = models.IntegerField(default=1, verbose_name='兑换份数')

    status = models.SmallIntegerField(default=STATUS_SUCCESS, verbose_name='状态')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='兑换时间')

    class Meta:
        db_table = 'mall_redemption'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user_id', 'created_at']),
            models.Index(fields=['user_id', 'category', 'created_at']),
        ]

    def __str__(self):
        return f'{self.redemption_no}'


class Voucher(models.Model):
    """券码（兑换成功派生，进「我的券包」）。"""
    STATUS_UNUSED = 1      # 未使用
    STATUS_USED = 2        # 已使用
    STATUS_EXPIRED = 3     # 已过期
    STATUS_LOCKED = 4      # 锁定中（被某笔待付款订单占用；支付成功转 USED，关单/退款释放回 UNUSED）

    STATUS_CHOICES = [
        (STATUS_UNUSED, '未使用'),
        (STATUS_USED, '已使用'),
        (STATUS_EXPIRED, '已过期'),
        (STATUS_LOCKED, '锁定中'),
    ]

    voucher_no = models.CharField(max_length=64, unique=True, verbose_name='券码')
    redemption_id = models.BigIntegerField(verbose_name='兑换单ID')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    item_id = models.BigIntegerField(verbose_name='商品ID')
    item_name = models.CharField(max_length=64, verbose_name='权益名称快照')
    category = models.SmallIntegerField(verbose_name='品类')

    # 抵扣快照（兑换时从 MallItem 写入，之后不随商品变更；仅代金券有值）
    value_fen = models.BigIntegerField(default=0, verbose_name='券面额快照(分)')
    threshold_fen = models.BigIntegerField(default=0, verbose_name='使用门槛快照(分,0=无门槛)')
    # 占用/核销所在订单：LOCKED/USED 期间记录 order_ext_no，回滚时清空
    order_ext_no = models.CharField(max_length=64, blank=True, default='', verbose_name='占用/核销订单号')

    status = models.SmallIntegerField(default=STATUS_UNUSED, verbose_name='状态')
    expire_at = models.DateTimeField(verbose_name='过期时间')
    used_at = models.DateTimeField(null=True, blank=True, verbose_name='使用时间')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'mall_voucher'
        ordering = ['-created_at', '-id']
        indexes = [
            models.Index(fields=['user_id', 'status']),
            models.Index(fields=['expire_at']),
            models.Index(fields=['order_ext_no']),
        ]

    def __str__(self):
        return self.voucher_no
