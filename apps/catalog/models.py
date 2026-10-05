"""catalog 数据模型：城市 / 影片 / 影院 / 影厅 / 排期。"""
from django.db import models


class City(models.Model):
    """城市。

    字段约定（以麻花 cityId 为准）：
    - city_code：麻花城市代码 cityId（数字，如成都=8），是后端调用麻花接口与落库的唯一标识。
    - std_code：国标行政区划码（如成都=510100），用于前端定位/展示，可空（部分麻花城市无对应国标）。
    """
    city_code = models.CharField(max_length=16, unique=True, verbose_name='麻花城市ID')
    city_name = models.CharField(max_length=32, verbose_name='城市名')
    pinyin = models.CharField(max_length=32, null=True, verbose_name='拼音')
    std_code = models.CharField(max_length=16, null=True, blank=True, verbose_name='国标行政区划码')
    hot = models.SmallIntegerField(default=0, verbose_name='热门')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'city'
        indexes = [models.Index(fields=['std_code'])]


class Movie(models.Model):
    """影片。"""
    STATUS_HOT = 1
    STATUS_COMING = 2
    STATUS_OFFLINE = 3

    up_movie_id = models.CharField(max_length=64, unique=True, verbose_name='麻花影片ID')
    name = models.CharField(max_length=128, verbose_name='片名')
    status = models.SmallIntegerField(verbose_name='1热映 2待映 3已下线')
    type = models.CharField(max_length=64, null=True, verbose_name='类型')
    duration = models.SmallIntegerField(null=True, verbose_name='时长(分钟)')
    rating = models.DecimalField(max_digits=3, decimal_places=1, null=True, verbose_name='评分')
    region = models.CharField(max_length=32, null=True, verbose_name='地区')
    language = models.CharField(max_length=32, null=True, verbose_name='语言')
    release_date = models.DateField(null=True, verbose_name='上映日期')
    director = models.CharField(max_length=128, null=True, verbose_name='导演')
    actors = models.CharField(max_length=512, null=True, verbose_name='主演')
    poster_url = models.CharField(max_length=512, null=True, verbose_name='海报')
    description = models.TextField(null=True, verbose_name='简介')
    want_count = models.IntegerField(default=0, verbose_name='想看人数')
    presale = models.SmallIntegerField(default=0, verbose_name='预售/特惠 1是 0否')
    no_show_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='麻花确认无排片时间(按影片查影院返回权威空集时打标)',
    )
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'movie'
        indexes = [models.Index(fields=['status', 'release_date'])]


class Cinema(models.Model):
    """影院。"""
    BUSINESS_ON = 1
    BUSINESS_OFF = 2
    BUSINESS_SOON_OFF = 3

    up_cinema_id = models.CharField(max_length=64, unique=True, verbose_name='麻花影院ID')
    name = models.CharField(max_length=128, verbose_name='影院名')
    city_code = models.CharField(max_length=16, verbose_name='城市代码')
    region = models.CharField(max_length=32, null=True, verbose_name='区域')
    brand = models.CharField(max_length=32, null=True, verbose_name='品牌')
    address = models.CharField(max_length=255, null=True, verbose_name='地址')
    lng = models.DecimalField(max_digits=10, decimal_places=6, null=True, verbose_name='经度')
    lat = models.DecimalField(max_digits=10, decimal_places=6, null=True, verbose_name='纬度')
    phone = models.CharField(max_length=32, null=True, verbose_name='电话')
    supports = models.CharField(max_length=255, null=True, verbose_name='服务标签')
    business_status = models.SmallIntegerField(default=1, verbose_name='1营业 2停业 3即将停业')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'cinema'
        indexes = [
            models.Index(fields=['city_code']),
            models.Index(fields=['lng', 'lat']),
        ]


class CinemaHall(models.Model):
    """影厅。"""
    cinema_id = models.BigIntegerField(verbose_name='影院ID')
    up_hall_id = models.CharField(max_length=64, verbose_name='麻花影厅ID')
    hall_name = models.CharField(max_length=64, verbose_name='影厅名')
    hall_type = models.CharField(max_length=32, null=True, verbose_name='IMAX/3D/4DX/普通')

    class Meta:
        db_table = 'cinema_hall'
        unique_together = [('cinema_id', 'up_hall_id')]


class Schedule(models.Model):
    """排片/场次。"""
    SELL_ON = 1
    SELL_OFF = 2

    up_schedule_id = models.CharField(max_length=64, unique=True, verbose_name='麻花排片ID')
    cinema_id = models.BigIntegerField(verbose_name='影院ID')
    movie_id = models.BigIntegerField(verbose_name='影片ID')
    hall_id = models.CharField(max_length=64, null=True, verbose_name='影厅ID')
    hall_name = models.CharField(max_length=64, null=True, verbose_name='影厅名')
    start_at = models.DateTimeField(verbose_name='开场时间')
    end_at = models.DateTimeField(null=True, verbose_name='散场时间')
    stopsell_at = models.DateTimeField(
        null=True, blank=True,
        verbose_name='停售时间(麻花特惠口径,空则回退start_at)',
    )
    show_type = models.CharField(max_length=16, null=True, verbose_name='2D/3D/IMAX')
    language = models.CharField(max_length=32, null=True, verbose_name='语言')
    min_price = models.BigIntegerField(default=0, verbose_name='最低价(分)')
    origin_price = models.BigIntegerField(null=True, blank=True, verbose_name='原价(分)')
    settle_price = models.BigIntegerField(null=True, verbose_name='结算价(分)')
    # 麻花原始价格快照（未上浮，分）：{price, fastPrice, maxSpeedPrice}，每次拉排片刷新。
    # min_price 存的是上浮后售价，本字段留原始成本口径供对账/审计。
    raw_price_json = models.JSONField(null=True, blank=True, verbose_name='麻花原始价快照')
    remain_seats = models.IntegerField(null=True, verbose_name='余票')
    refundable = models.SmallIntegerField(default=0, verbose_name='可退')
    endorseable = models.SmallIntegerField(default=0, verbose_name='可改签')
    sell_status = models.SmallIntegerField(default=1, verbose_name='1可售 2停售')
    snapshot_at = models.DateTimeField(verbose_name='同步时间')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'schedule'
        indexes = [
            models.Index(fields=['cinema_id', 'movie_id', 'start_at']),
            models.Index(fields=['movie_id', 'start_at']),
        ]


class MarkupRule(models.Model):
    """上浮加价规则（定价中枢，见 apps/catalog/markup.py 解析器）。

    维度字段留空/NULL = 该维度不限；命中即停，按 priority 升序。
    is_fallback=1 为兜底规则（其它规则均不满足时用它）；无兜底则回退全局
    settings.PRICE_MARKUP_RATE（比例），保证与改造前行为等价。
    """
    MODE_RATE = 'rate'
    MODE_FLAT = 'flat'
    MODE_CHOICES = [(MODE_RATE, '售价比例'), (MODE_FLAT, '固定加价(分/张)')]

    name = models.CharField(max_length=64, verbose_name='规则名')
    priority = models.IntegerField(default=100, verbose_name='优先级(小者先)')
    is_active = models.SmallIntegerField(default=1, verbose_name='1启用 0停用')
    is_fallback = models.SmallIntegerField(default=0, verbose_name='是否兜底规则')

    # —— 匹配维度（空=不限）——
    movie_ids = models.JSONField(null=True, blank=True, verbose_name='影片ID列表')
    brands = models.JSONField(null=True, blank=True, verbose_name='院线品牌列表')
    city_codes = models.JSONField(null=True, blank=True, verbose_name='城市码列表')
    hall_types = models.JSONField(null=True, blank=True, verbose_name='影厅类型/show_version列表')
    weekday_in = models.JSONField(null=True, blank=True, verbose_name='星期几(1-7)列表')
    hour_from = models.SmallIntegerField(null=True, blank=True, verbose_name='时段起(0-23)')
    hour_to = models.SmallIntegerField(null=True, blank=True, verbose_name='时段止(0-23)')

    # —— 加价方式 ——
    mode = models.CharField(max_length=8, default=MODE_RATE, verbose_name='加价方式')
    rate = models.DecimalField(null=True, blank=True, max_digits=6, decimal_places=4,
                               verbose_name='比例(0.12=12%)')
    flat_fen = models.BigIntegerField(null=True, blank=True, verbose_name='固定加价(分/张)')

    effective_from = models.DateTimeField(null=True, blank=True, verbose_name='生效起')
    effective_to = models.DateTimeField(null=True, blank=True, verbose_name='生效止')

    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'markup_rule'
        ordering = ['priority', 'id']

    def __str__(self):
        return f'{self.id}-{self.name}'
