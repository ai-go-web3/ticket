"""麻花对接镜像模型：账户 / 充值 / 回调日志 / OCR 识别会话。"""
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


class OcrSession(models.Model):
    """图片识别报价会话（C 端首页截图购票：识别→匹配场次→补座位→喂 create_order）。

    资金/定价口径（本次决策）：
    - `evaluate_fen`：麻花 OCR 返回的座位特惠估价（元×100，**成本**，未上浮）。
    - `sell_fen`：把 `evaluate_fen` 走我方上浮规则（apps.catalog.markup）后的**售价**，
      即本单成交价/应付，也是回传前端展示的我方价。flat 模式按每张加、故按座位数放大。
    - `face_total_fen`：座位原价合计（省钱对比的"原价"侧）。识别阶段取 OCR 的 ocrSeatTotalPrice，
      resolve 阶段用实时座位接口拉到的 Σ原价刷新（更准）。
    - `save_fen`：`face_total_fen - sell_fen` = 本单省了多少（我方售价相对原价）。
    图片本身**不落库**（隐私，用后即焚），仅存 img_sha256 指纹 + 字节数。金额单位统一"分"。
    """
    ST_RECOG = 0     # 识别中
    ST_SUCCESS = 1   # 唯一匹配到在售排片 → 可下单
    ST_AMBIG = 2     # 识别成功但候选多条不唯一 → 需用户选定场次
    ST_NOMATCH = 3   # 识别到部分信息但未匹配到排片/座位
    ST_FAIL = 4      # 业务/参数/解析失败（图片不可用、余额不足、服务异常…）
    STATUS_CHOICES = [
        (ST_RECOG, '识别中'), (ST_SUCCESS, '唯一匹配'), (ST_AMBIG, '多候选'),
        (ST_NOMATCH, '未匹配'), (ST_FAIL, '失败'),
    ]

    out_biz_no = models.CharField(max_length=64, unique=True, verbose_name='幂等号')
    user_id = models.BigIntegerField(verbose_name='用户ID')
    img_sha256 = models.CharField(max_length=64, verbose_name='图片指纹(不落原图)')
    img_size = models.IntegerField(default=0, verbose_name='图片字节数')

    rtn_code = models.CharField(max_length=16, null=True, verbose_name='麻花码')
    rtn_msg = models.CharField(max_length=128, null=True, verbose_name='麻花文案')
    status = models.SmallIntegerField(choices=STATUS_CHOICES, default=ST_RECOG, verbose_name='状态')
    error_stage = models.CharField(max_length=16, null=True, verbose_name='归一化失败阶段')

    # 麻花侧 ID（=内部 up_*_id join 键）
    up_film_id = models.CharField(max_length=64, null=True, verbose_name='麻花影片ID')
    up_cinema_id = models.CharField(max_length=64, null=True, verbose_name='麻花影院ID')
    up_show_id = models.CharField(max_length=64, null=True, verbose_name='麻花排片ID')
    film_std_id = models.CharField(max_length=64, null=True, verbose_name='影片标准ID')
    cinema_std_id = models.CharField(max_length=64, null=True, verbose_name='影院标准ID')

    # join 后的内部主键（匹配不到留空）
    movie_id = models.BigIntegerField(null=True, verbose_name='内部影片ID')
    cinema_id = models.BigIntegerField(null=True, verbose_name='内部影院ID')
    schedule_id = models.BigIntegerField(null=True, verbose_name='内部排片ID')

    recognized_json = models.JSONField(null=True, verbose_name='识别信息原文')
    schedule_json = models.JSONField(null=True, verbose_name='场次信息原文')
    seats_json = models.JSONField(null=True, verbose_name='识别座位[{seatNo,price元}]')
    candidates_json = models.JSONField(null=True, verbose_name='候选排片[]')
    raw_response_json = models.JSONField(null=True, verbose_name='完整rtnData留档')

    seat_count = models.IntegerField(default=0, verbose_name='座位数(flat上浮用)')
    evaluate_fen = models.BigIntegerField(null=True, verbose_name='麻花特惠估价=成本(分,未上浮)')
    sell_fen = models.BigIntegerField(null=True, verbose_name='上浮后售价=成交价/应付(分)')
    face_total_fen = models.BigIntegerField(null=True, verbose_name='座位原价合计(分)')
    save_fen = models.BigIntegerField(null=True, verbose_name='省(分,原价-售价)')
    matched_index = models.SmallIntegerField(null=True, verbose_name='候选选定下标')

    charged_fen = models.IntegerField(default=0, verbose_name='本次计费(分,成功=1)')
    cost_ms = models.IntegerField(null=True, verbose_name='调用耗时(ms)')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'ocr_session'
        indexes = [
            models.Index(fields=['user_id']),
            models.Index(fields=['status', 'created_at']),
            models.Index(fields=['created_at']),
            models.Index(fields=['up_show_id']),
            models.Index(fields=['img_sha256']),
        ]

    def __str__(self):
        return f'{self.id}-{self.out_biz_no}-{self.status}'
