"""OCR 图片识别报价服务（C 端首页截图购票闭环）。

链路：识别（调麻花 OCR）→ join 内部场次 → 用实时座位接口补 row/col/原价 →
把麻花 evaluatePrice（座位特惠估价，成本）按我方上浮规则(markup)加成售价 →
产出 {scheduleId, seats(salePrice=原价), discountAmount(=Σ原价-上浮后售价)} 直接喂现成
POST /api/v1/order/create（create_order 不改：pay_amount = ΣsalePrice + 服务费0 - discount = 上浮后售价）。

定价（本次决策）：成交价 = 麻花 evaluatePrice **经我方上浮规则加成后的售价**（sell_fen）。
- rate 模式：sell = round(evaluate × (1+rate))（比例对总价与逐座等价）。
- flat 模式：markup 是每张加 flat_fen，evaluatePrice 是整单估价，故 sell = evaluate + flat_fen × seat_count。
上浮后售价高于原价合计时兜到原价（不出现反向优惠），与全链路口径一致。
"""
import base64
import hashlib
import logging
import time

from datetime import timedelta

from django.utils import timezone

from apps.catalog import markup
from apps.catalog import services as catalog_services
from apps.catalog.models import Schedule
from apps.common.response import BizError, ErrorCode
from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE
from apps.upadapter.models import OcrSession
from apps.upadapter.token import get_token

logger = logging.getLogger('app')

OCR_COST_FEN = 1              # 0.01 元 = 1 分/次（成功才扣，麻花侧扣费；本值仅对账镜像用）
# 全局限频：改为按「近窗口内 OcrSession 建行数」计数（走 MySQL，worker 无关），
# 取代原进程内 LocMem 令牌桶（N 个 worker 各记各的 → 实际 N×60/min，见 docs §6）。
OCR_RATE_WINDOW_SEC = 60      # 滑动窗口秒数
OCR_RATE_LIMIT = 60           # 窗口内全局上限（麻花硬限频）
OCR_USER_DAILY_LIMIT = 10     # 每用户每日识别次数上限（控成本/防刷；走 DB 计数，worker 无关）
OCR_DEDUP_WINDOW_SEC = 600    # 同图去重窗口：同用户近 N 秒识别过同图(sha 相同)则复用，不再调麻花（省 0.01 元 / 不占额度与限频）
IMG_MAX_BYTES = 5 * 1024 * 1024

# error_stage → 面向用户的文案（对齐原型失败A「没匹配到」/失败B「看不清」两条兜底）
_ERR_MAP = {
    'no_schedule': '没匹配到在售场次',
    'no_seat': '识别到影片了，但没匹配上座位，请换含座位的截图',
    'no_cinema': '没找到这家影院，请换更清晰的截图',
    'blurry': '这张图看不太清，换一张试试',
    'insufficient_balance': '服务繁忙，请稍后再试',   # 麻花余额不足，对用户不暴露
    'not_merchant': '功能暂未开放',
    'rate_limit': '操作太频繁，请稍后再试',
    'unknown': '识别失败，请重试或手动选片',
}


def err_msg(sess):
    """取会话失败阶段的用户文案。"""
    return _ERR_MAP.get(sess.error_stage or 'unknown', _ERR_MAP['unknown'])


# ---- 归一化 / 校验 ----
def _fen(yuan):
    if yuan in (None, ''):
        return None
    try:
        return int(round(float(yuan) * 100))
    except (TypeError, ValueError):
        return None


def _normalize_fail(msg, data):
    """把麻花失败码/文案归一化为 error_stage。解析阶段数字码见接口文档 rtnCode 说明。"""
    m = (msg or '')
    if '未匹配到排片' in m or '未找到场次' in m or '未找到场次时间' in m:
        return 'no_schedule'
    if '未匹配到座位' in m or '未找到座位' in m:
        return 'no_seat'
    if '未找到影院' in m or '未匹配到影院' in m:
        return 'no_cinema'
    if '未能识别' in m or '图片无数据' in m or '识别失败' in m:
        return 'blurry'
    if '余额不足' in m:
        return 'insufficient_balance'
    if '商户' in m:
        return 'not_merchant'
    return 'unknown'


def _normalize_fail_code(code):
    """识别解析阶段的数字码（字符串）→ error_stage。"""
    return {
        '0': 'blurry',        # 图片无数据
        '1': 'no_seat',       # 未找到座位
        '2': 'no_schedule',   # 未找到场次时间
        '3': 'no_schedule',   # 未找到电影（视作未匹配到场次上下文）
        '4': 'no_cinema',     # 未找到/未匹配到影院
        '5': 'no_schedule',   # 未找到场次
        '6': 'blurry',        # 识别失败
        '7': 'no_seat',       # 未匹配到座位
    }.get(str(code), 'unknown')


def _enforce_rate_limit():
    """全局限频：按「近窗口内 OcrSession 建行数」计数（走 MySQL，worker 无关）。

    取代原进程内 LocMem 令牌桶（N 个 worker 各记各的 → 实际 N×60/min，docs §6）。
    放行即建行、建行即被下次计数，故窗口内计数≈已放行请求数。软限：并发边界可能多放行
    少量（读-改非原子），低峰量 + 每次调用真金白银，误差可接受；要硬限可叠 GET_LOCK。
    """
    win_start = timezone.now() - timedelta(seconds=OCR_RATE_WINDOW_SEC)
    cnt = OcrSession.objects.filter(created_at__gte=win_start).count()
    if cnt >= OCR_RATE_LIMIT:
        raise BizError(_ERR_MAP['rate_limit'], code=ErrorCode.UP_RATE_LIMIT)


def _enforce_user_daily_limit(user_id):
    """每用户每日识别次数上限（走 DB 计数，worker 无关；控成本/防刷）。

    一条 OcrSession ≈ 一次已发起的识别请求（校验/全局限频在建行前已拦截），
    故按当日建行数即等于当日已用次数。USE_TZ=False，日界按本地 0 点。
    """
    from datetime import datetime, date, time as _dtime
    day_start = datetime.combine(date.today(), _dtime.min)
    used = OcrSession.objects.filter(user_id=user_id, created_at__gte=day_start).count()
    if used >= OCR_USER_DAILY_LIMIT:
        raise BizError('今天识别次数用完啦，明天再来，或先手动选片购票', code=ErrorCode.OCR_DAILY_LIMIT)


def _find_reusable(user_id, img_sha256):
    """同图去重：同用户近窗口内已拿到麻花应答的同图会话 → 复用，不再调麻花。

    仅复用终态成功/多候选/未匹配（ST_SUCCESS/AMBIG/NOMATCH）——这些都有可展示的识别结果；
    ST_FAIL（余额不足/网络等瞬态）与 ST_RECOG（进行中）不复用，应重新识别。
    座位实时可售性不在此判定（resolve 阶段仍会 pull_seats 复核），复用旧会话不会误锁已售座位。
    """
    win_start = timezone.now() - timedelta(seconds=OCR_DEDUP_WINDOW_SEC)
    return (
        OcrSession.objects
        .filter(
            user_id=user_id, img_sha256=img_sha256,
            status__in=(OcrSession.ST_SUCCESS, OcrSession.ST_AMBIG, OcrSession.ST_NOMATCH),
            created_at__gte=win_start,
        )
        .order_by('-id').first()
    )


def _validate_img(img_base64):
    if not img_base64:
        raise BizError('请先选择图片', code=ErrorCode.PARAM_ERROR)
    if img_base64[:5].lower() == 'data:':
        img_base64 = img_base64.split(',', 1)[-1]   # 容错：剥 data:image/...;base64, 前缀
    try:
        raw = base64.b64decode(img_base64)
    except Exception:  # noqa: BLE001
        raise BizError('图片格式不正确', code=ErrorCode.PARAM_ERROR)
    if len(raw) > IMG_MAX_BYTES:
        raise BizError('图片过大（>5M），请压缩后再传', code=ErrorCode.PARAM_ERROR)
    return img_base64, hashlib.sha256(raw).hexdigest(), len(raw)


# ---- 定价 ----
def _uplift_evaluate_total(evaluate_fen, seat_count, rule):
    """把整单 evaluatePrice（成本，分）按命中上浮规则加成售价（分）。

    rate：直接对总价加成（比例对逐座/整单等价）。
    flat：markup 定义是每张加 flat_fen，evaluatePrice 是整单，故按座位数放大。
    """
    if not evaluate_fen or evaluate_fen <= 0:
        return evaluate_fen
    mode, rate, flat_fen = rule
    if mode == markup.MODE_FLAT:
        return int(evaluate_fen) + int(flat_fen) * max(int(seat_count or 1), 1)
    return int(round(evaluate_fen * (1 + rate)))


def _compute_sell_fen(sess, schedule):
    """命中场次的上浮规则，计算 sell_fen（成交价/应付）并回存。返回 sell_fen。

    上浮后售价不得高于原价合计（有原价时兜住，与全链路"售价≤原价"一致）。
    """
    if sess.evaluate_fen is None:
        sess.sell_fen = None
        return None
    rule = markup.resolve_for_schedule(schedule) if schedule else None
    sell = _uplift_evaluate_total(sess.evaluate_fen, sess.seat_count, rule)
    if sess.face_total_fen and sess.face_total_fen > 0:
        sell = min(sell, sess.face_total_fen)
    sess.sell_fen = sell
    if sess.face_total_fen is not None and sell is not None:
        sess.save_fen = max(0, sess.face_total_fen - sell)
    return sell


# ---- 识别 ----
def recognize(user_id, out_biz_no, img_base64):
    """调麻花 OCR + join 内部场次 + 定价 + 落库。返回 OcrSession（不抛业务异常，失败态入库）。"""
    img_base64, sha, size = _validate_img(img_base64)

    # 1) 同图去重：同用户近窗口已识别过同图 → 直接复用旧会话，不调麻花
    #    （省 0.01 元，且不占全局限频/每日额度——因为提前返回，不建行、不计入计数）
    reusable = _find_reusable(user_id, sha)
    if reusable:
        return reusable

    # 2) 全局限频（DB 窗口计数，worker 无关）
    _enforce_rate_limit()
    # 3) 每用户每日上限
    _enforce_user_daily_limit(user_id)

    sess = OcrSession.objects.create(
        out_biz_no=out_biz_no, user_id=user_id, img_sha256=sha, img_size=size,
        status=OcrSession.ST_RECOG)

    t0 = time.time()
    client = MahuaClient()
    token = get_token()
    try:
        code, msg, data = client.ocr_evaluate_price(token, img_base64)
    except Exception as exc:  # noqa: BLE001 - 网络/超时等，归一为失败态
        logger.exception('mahua.ocr 调用异常 user=%s', user_id)
        sess.status = OcrSession.ST_FAIL
        sess.error_stage = 'unknown'
        sess.rtn_msg = str(exc)[:128]
        sess.cost_ms = int((time.time() - t0) * 1000)
        sess.save()
        return sess
    sess.cost_ms = int((time.time() - t0) * 1000)
    sess.rtn_code = code
    sess.rtn_msg = str(msg)[:128] if msg else None

    if code != SUCCESS_CODE or not isinstance(data, dict):
        sess.status = OcrSession.ST_FAIL
        sess.error_stage = _normalize_fail(sess.rtn_msg, data)
        # 解析阶段数字码（rtnCode 直接是 '0'..'7'）
        if sess.error_stage == 'unknown':
            sess.error_stage = _normalize_fail_code(code)
        sess.raw_response_json = data if isinstance(data, (dict, list)) else None
        sess.save()
        return sess

    sess.charged_fen = OCR_COST_FEN
    sess.raw_response_json = data
    film = data.get('film') or {}
    cinema = data.get('cinema') or {}
    sched = data.get('schedule') or {}
    recognized = data.get('recognized') or {}
    cands = data.get('candidates') or []

    sess.up_film_id = str(film.get('filmId') or '') or None
    sess.up_cinema_id = str(cinema.get('cinemaId') or '') or None
    sess.up_show_id = sched.get('showId') or None
    sess.film_std_id = film.get('filmStandardId') or None
    sess.cinema_std_id = cinema.get('standardId') or None
    sess.recognized_json = recognized
    sess.schedule_json = sched or None
    sess.seats_json = data.get('seats') or None
    sess.candidates_json = cands or None

    sess.seat_count = recognized.get('seatNum') or len(data.get('seats') or [])
    sess.face_total_fen = _fen(recognized.get('ocrSeatTotalPrice'))
    sess.evaluate_fen = _fen((data.get('estimate') or {}).get('evaluatePrice'))

    _resolve_internal(sess, sched, cinema, film, cands)
    # 匹配到场次才算得出上浮售价（AMBIG/NOMATCH 无最终场次，留待 resolve）
    if sess.status == OcrSession.ST_SUCCESS and sess.schedule_id:
        _compute_sell_fen(sess, Schedule.objects.filter(id=sess.schedule_id).first())
    else:
        # 未定场次：仅按原价-估价给个粗略省额展示（不含上浮），前端提示需确认场次
        if sess.face_total_fen is not None and sess.evaluate_fen is not None:
            sess.save_fen = max(0, sess.face_total_fen - sess.evaluate_fen)
    sess.save()
    return sess


def _resolve_internal(sess, sched, cinema, film, cands):
    """join 内部实体 + 定状态。主路径：up_show_id 精确命中在售排片。"""
    if sess.up_show_id:
        s = Schedule.objects.filter(up_schedule_id=sess.up_show_id, deleted=0).first()
        if s:
            sess.schedule_id, sess.cinema_id, sess.movie_id = s.id, s.cinema_id, s.movie_id
            try:
                catalog_services.ensure_sellable(s)   # 与列表/选座/建单同一可售口径
                sess.status = OcrSession.ST_SUCCESS
                return
            except BizError:
                pass  # 停售/过期 → 回退候选/未匹配
    # showId 命中失败：尝试用 candidates（含影院名/片名/时间）反查。candidates 无 showId，
    # 仅在"顶层 cinema/film + 候选 showTime"能唯一确定场次时收敛，否则多候选/未匹配。
    joined = []
    for i, c in enumerate(cands):
        s = _lookup_schedule_by_meta(sess, c)
        if s:
            joined.append((i, s))
    if len(joined) == 1:
        i, s = joined[0]
        sess.schedule_id, sess.cinema_id, sess.movie_id = s.id, s.cinema_id, s.movie_id
        sess.matched_index = i
        try:
            catalog_services.ensure_sellable(s)
            sess.status = OcrSession.ST_SUCCESS
        except BizError:
            sess.status = OcrSession.ST_NOMATCH
            sess.error_stage = 'no_schedule'
    elif len(joined) > 1:
        sess.status = OcrSession.ST_AMBIG      # 待用户选定后由 resolve 回填
    else:
        sess.status = OcrSession.ST_NOMATCH
        sess.error_stage = 'no_schedule'


def _lookup_schedule_by_meta(sess, cand):
    """candidates 单条（影院名/片名/时间/影厅）→ 反查唯一在售 Schedule。

    精度折衷：candidates 不带 showId，用「顶层影院/影片的麻花 ID 命中内部 Cinema/Movie +
    候选 showTime 对齐 Schedule.start_at」定位。命中多条返回第一条由上层判 AMBIG。
    """
    show_time = cand.get('showTime')
    if not show_time:
        return None
    from datetime import datetime
    from django.utils import timezone
    dt = None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S'):
        try:
            dt = datetime.strptime(show_time, fmt)
            break
        except ValueError:
            continue
    if dt is None:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    qs = Schedule.objects.filter(deleted=0, start_at=dt)
    if sess.cinema_id:
        qs = qs.filter(cinema_id=sess.cinema_id)
    elif sess.up_cinema_id:
        from apps.catalog.models import Cinema
        cid = Cinema.objects.filter(up_cinema_id=sess.up_cinema_id, deleted=0).values_list('id', flat=True).first()
        if cid:
            qs = qs.filter(cinema_id=cid)
    if sess.movie_id:
        qs = qs.filter(movie_id=sess.movie_id)
    elif sess.up_film_id:
        from apps.catalog.models import Movie
        mid = Movie.objects.filter(up_movie_id=sess.up_film_id, deleted=0).values_list('id', flat=True).first()
        if mid:
            qs = qs.filter(movie_id=mid)
    return qs.first()


# ---- 解析成 create_order 入参 ----
def resolve(sess_id, user_id, chosen_index=None):
    """把识别会话翻译成 create_order 可直接消费的入参（含实时座位补全 + 上浮后成交价）。

    - chosen_index：多候选时用户选定下标 → 回填并精确 join 该候选场次。
    - 复用现成实时座位接口 pull_seats 校验座位在该场次仍可售、取 row/col/name/seatId/原价。
    - 成交价 = evaluatePrice 上浮后售价（sell_fen）；用 discountAmount 把 create_order 实付对齐到它
      （每座 salePrice=原价，discountAmount = Σ原价 - sell_fen）。create_order 不改。
    """
    sess = OcrSession.objects.filter(id=sess_id).first()
    if not sess or sess.user_id != user_id:
        raise BizError('识别会话不存在', code=ErrorCode.NOT_FOUND)

    # 多候选：用户选定后精确回填场次
    if chosen_index is not None and sess.candidates_json:
        try:
            idx = int(chosen_index)
        except (TypeError, ValueError):
            raise BizError('候选下标不合法', code=ErrorCode.PARAM_ERROR)
        if 0 <= idx < len(sess.candidates_json):
            sess.matched_index = idx
            s = _lookup_schedule_by_meta(sess, sess.candidates_json[idx])
            if s:
                sess.schedule_id, sess.cinema_id, sess.movie_id = s.id, s.cinema_id, s.movie_id
                sess.status = OcrSession.ST_SUCCESS
                sess.save(update_fields=['matched_index', 'schedule_id', 'cinema_id', 'movie_id', 'status'])

    if sess.status != OcrSession.ST_SUCCESS or not sess.schedule_id:
        raise BizError(err_msg(sess) if sess.error_stage else '请先确认场次后再下单',
                       code=ErrorCode.UP_ERROR)
    if sess.evaluate_fen is None:
        raise BizError('未获取到估价，请重新识别', code=ErrorCode.UP_ERROR)

    schedule = Schedule.objects.filter(id=sess.schedule_id, deleted=0).first()
    if not schedule:
        raise BizError('场次不存在或已下架，请重新识别', code=ErrorCode.UP_ERROR)
    catalog_services.ensure_sellable(schedule)

    # 实时座位接口：只用于「校验座位在该场次仍可售 + 取 row/col/name/seatId/原价」，不取它的上浮价
    rows, _restr, _minp = catalog_services.pull_seats(schedule.up_schedule_id)
    index = {str(seat.get('name')): seat for r in rows for seat in r.get('seats', [])}
    seats = []
    sum_face = 0
    for s in (sess.seats_json or []):
        name = str(s.get('seatNo'))
        hit = index.get(name)
        if not hit:
            raise BizError('识别到的座位在该场次已不存在/已售出，请重新选座', code=ErrorCode.UP_ERROR)
        if hit.get('status') != 0:     # 0 才可售
            raise BizError(f'座位 {name} 当前不可售，请重新选座', code=ErrorCode.UP_ERROR)
        face = int(hit.get('price') or 0)   # 座位原价（分，未上浮）
        seats.append({**hit, 'salePrice': face})   # 每座售价=原价，票面合计=Σ原价
        sum_face += face

    # 用实时原价合计刷新 face，再按命中场次规则算上浮后售价 sell_fen
    sess.face_total_fen = sum_face or sess.face_total_fen
    sess.seat_count = len(seats) or sess.seat_count
    sell = _compute_sell_fen(sess, schedule)
    sess.save(update_fields=['face_total_fen', 'seat_count', 'sell_fen', 'save_fen'])
    if sell is None:
        raise BizError('未计算出处方售价，请重新识别', code=ErrorCode.UP_ERROR)

    discount = max(0, sum_face - sell)
    return {
        'scheduleId': schedule.id,
        'seats': seats,                    # 含 row/col/name/seatId/salePrice(=原价)
        'discountAmount': discount,        # 使 create_order 实付 = sell_fen
        'sellAmountFen': sell,             # 上浮后售价 = 成交价/应付（回传前端展示）
        'faceTotalFen': sum_face,          # 原价合计（省钱对比"原价"侧）
        'saveFen': sess.save_fen,
        'evaluateFen': sess.evaluate_fen,  # 麻花原始估价（成本，仅留档；前端不必展示）
        'status': sess.status,
    }
