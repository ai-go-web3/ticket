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
