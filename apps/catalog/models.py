"""catalog 数据模型：城市 / 影片 / 影院 / 影厅 / 排期。"""
import uuid

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
    cinema_ids = models.JSONField(null=True, blank=True, verbose_name='影院ID列表(内部Cinema.id)')
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


class RecommendSection(models.Model):
    """C 端首页「本月推荐」栏目级设置（单例行）。

    运营端可改栏目标题、最多展示条数、以及「无启用推荐位时是否回退自动 Top4」。
    取用见 load()：无记录时自动创建一份默认配置，保证单例存在。
    """
    title = models.CharField(max_length=32, default='本月推荐', verbose_name='栏目标题')
    max_show = models.SmallIntegerField(default=6, verbose_name='最多展示条数')
    fallback_top4 = models.SmallIntegerField(default=1, verbose_name='空位回退自动Top4 1是 0否')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'recommend_section'

    @classmethod
    def load(cls):
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj

    def __str__(self):
        return f'recommend-section-{self.id}'


class RecommendSlot(models.Model):
    """首页推荐位轮播的一格：影片或自定义 banner（混合轮播）。

    - slot_type='movie'：关联内部 Movie.id（非麻花 up_movie_id），海报/片名/角标由影片实时派生；
      可另配 banner_url(横版封面) 供首页横版轮播使用，缺省时前端回退品牌渐变占位。
    - slot_type='banner'：使用 title/subtitle/cta + bg(渐变) 或 image_url(自定义底图)。
    - city_code：'all' 或空 = 全国；否则为麻花城市ID（与 City.city_code / MarkupRule.city_codes 同口径）。
    - enabled + sort + 生效时间窗口决定 C 端展示；软删保留审计。
    """
    TYPE_MOVIE = 'movie'
    TYPE_BANNER = 'banner'
    TYPE_CHOICES = [(TYPE_MOVIE, '影片'), (TYPE_BANNER, '横幅')]

    LINK_MOVIE = 'movie'
    LINK_CINEMA = 'cinema'
    LINK_COUPON = 'coupon'
    LINK_ACTIVITY = 'activity'
    LINK_NONE = 'none'

    slot_type = models.CharField(max_length=8, default=TYPE_MOVIE, verbose_name='movie/banner')
    movie_id = models.BigIntegerField(null=True, blank=True, verbose_name='内部影片ID(Movie.id)')
    title = models.CharField(max_length=128, null=True, blank=True, verbose_name='标题(banner用)')
    subtitle = models.CharField(max_length=255, null=True, blank=True, verbose_name='副标题(banner用)')
    cta = models.CharField(max_length=64, null=True, blank=True, verbose_name='按钮文案(banner用)')
    bg = models.CharField(max_length=255, null=True, blank=True, verbose_name='底图渐变(banner用)')
    image_url = models.CharField(max_length=512, null=True, blank=True, verbose_name='自定义底图URL')
    banner_url = models.CharField(max_length=512, null=True, blank=True, verbose_name='横版封面URL(影片位轮播用)')
    badge = models.CharField(max_length=32, null=True, blank=True, verbose_name='角标文案')
    badge_color = models.CharField(max_length=8, default='pink', verbose_name='pink/blue/gold')
    city_code = models.CharField(max_length=16, default='all', verbose_name='all或麻花城市ID')
    link_type = models.CharField(max_length=16, default=LINK_MOVIE, verbose_name='跳转类型')
    link_ref = models.CharField(max_length=128, null=True, blank=True, verbose_name='跳转引用')
    sort = models.IntegerField(default=0, verbose_name='排序(小者前)')
    enabled = models.SmallIntegerField(default=1, verbose_name='1启用 0停用')

    effective_from = models.DateTimeField(null=True, blank=True, verbose_name='生效起')
    effective_to = models.DateTimeField(null=True, blank=True, verbose_name='生效止')

    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'recommend_slot'
        ordering = ['sort', 'id']
        indexes = [models.Index(fields=['enabled', 'deleted'])]

    def __str__(self):
        return f'{self.id}-{self.slot_type}-{self.title or self.movie_id}'


def _gen_media_key():
    """MediaAsset 公开访问用的不可猜测 key（32 位 hex）。"""
    return uuid.uuid4().hex


class MediaAsset(models.Model):
    """图片资源：运营上传的小图直接存 MySQL（数据量少，免外部 OSS/文件存储）。

    - key：uuid4 hex，公开服务端点用它寻址（`/api/v1/catalog/media/<key>`），不可猜测。
    - data：BinaryField（MySQL 映射为 longblob）。
    - content_type / size：回写与校验用；服务端据 content_type 返回正确 MIME。
    - 内容按 key 不可变，服务端可长缓存。软删/审计暂不做（量小，删除即物理删行）。
    """
    key = models.CharField(max_length=32, unique=True, default=_gen_media_key, verbose_name='公开key')
    name = models.CharField(max_length=255, null=True, blank=True, verbose_name='原始文件名')
    content_type = models.CharField(max_length=64, verbose_name='MIME')
    size = models.IntegerField(default=0, verbose_name='字节数')
    width = models.IntegerField(null=True, blank=True, verbose_name='宽(px)')
    height = models.IntegerField(null=True, blank=True, verbose_name='高(px)')
    uploaded_by = models.BigIntegerField(null=True, blank=True, verbose_name='上传管理员ID')
    data = models.BinaryField(verbose_name='图片二进制')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')

    class Meta:
        db_table = 'media_asset'
        ordering = ['-id']
        indexes = [models.Index(fields=['key'])]

    @property
    def media_path(self):
        """相对路径（不含域名）；存进 banner_url/image_url 供各端按已知后端域名解析。"""
        return f'/api/v1/catalog/media/{self.key}'

    def __str__(self):
        return f'{self.key}-{self.content_type}-{self.size}B'
