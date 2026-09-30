"""catalog 数据同步服务：麻花回调增量入库（电影 / 排片 / 城市）。"""
import logging
from datetime import timedelta

from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.catalog.models import City, Movie, Cinema, Schedule
from apps.catalog.city_mapping import (
    MAHUA_CITY_MAP, HOT_CITY_IDS, mahua_to_std,
)

logger = logging.getLogger('app')


def sync_cities(city_list):
    """同步麻花城市列表入库。

    以麻花 cityId 为权威主键；std_code 由 city_mapping 静态表补齐（接口不返回国标码）。
    city_list: 麻花城市接口返回的 rtnData 数组，元素含 cityId/cityName/cityPinyin。
    """
    count = 0
    for item in city_list:
        city_id = str(item.get('cityId', ''))
        if not city_id:
            continue
        name = item.get('cityName', '') or city_mapping_city_name(city_id)
        defaults = {
            'city_name': name,
            'pinyin': item.get('cityPinyin'),
            'std_code': mahua_to_std(city_id) or None,
        }
        # 热门城市标记（在 HOT_CITY_IDS 中的置为热门）
        if city_id in HOT_CITY_IDS:
            defaults['hot'] = 1
        _, created = City.objects.update_or_create(city_code=city_id, defaults=defaults)
        count += 1
    return count


def city_mapping_city_name(city_id):
    """从静态映射表兜底取城市名（麻花接口缺 name 时）。"""
    from apps.catalog.city_mapping import city_name as _name
    return _name(city_id)


def resolve_city_code(std_or_mahua):
    """把前端传入的城市标识解析为麻花 cityId。

    前端可能传国标码（std_code）或麻花 cityId，统一归一为麻花 cityId（后端主键）。
    """
    from apps.catalog.city_mapping import std_to_mahua
    v = str(std_or_mahua)
    # 已是麻花 cityId（在映射表中）
    if v in MAHUA_CITY_MAP:
        return v
    # 是国标码，转麻花 cityId
    m = std_to_mahua(v)
    return m if m else v


def _parse_price(value):
    """麻花金额(元) -> 分(int)。"""
    try:
        return int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None


def _parse_date(value):
    """解析日期字符串，失败返回 None。"""
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def _parse_datetime(value):
    """解析时间字符串，失败返回 None。"""
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 场次「可售」口径 —— 唯一真相源
#
# 我方放单不传 model，麻花默认 0-特惠模式，故停售线取特惠停售时间 stopsell_at；
# stopsell_at 缺失（历史数据 / 报文未给）时回退 start_at，保证不会比修复前更宽松。
# 列表过滤(sellable_schedules) 与 单场校验(ensure_sellable) 共用此口径，
# 避免「列表不显示、但直接调接口仍能下单」的口径漂移。
# ---------------------------------------------------------------------------

def sell_deadline(schedule):
    """单场次的实际停售时刻：stopsell_at 优先，缺失回退 start_at。"""
    return getattr(schedule, 'sell_deadline_at', None) or schedule.stopsell_at or schedule.start_at


def sellable_schedules(now=None):
    """未删 + 在售 + 未过停售线 的场次查询集（列表用）。"""
    moment = now or timezone.now()
    return (
        Schedule.objects
        .annotate(sell_deadline_at=Coalesce('stopsell_at', 'start_at'))
        .filter(deleted=0, sell_status=Schedule.SELL_ON, sell_deadline_at__gt=moment)
    )


def ensure_sellable(schedule, now=None):
    """单场次可售校验；不可售抛 BizError。用于选座/锁座/建单等写操作前置硬拦截。"""
    from apps.common.response import BizError
    moment = now or timezone.now()
    if schedule is None or schedule.deleted:
        raise BizError('场次不存在', code=40400)
    if schedule.sell_status != Schedule.SELL_ON:
        raise BizError('场次已停售，请选择其他场次')
    deadline = sell_deadline(schedule)
    if deadline and deadline <= moment:
        raise BizError('该场次已过停售时间，请选择其他场次')
    return schedule


def upsert_movie_from_callback(data):
    """电影增量回调入库（字段映射 §6.3）。"""
    movie_id = str(data.get('id', ''))
    if not movie_id:
        return None

    rating = data.get('grade')
    defaults = {
        'name': data.get('name', ''),
        'duration': data.get('duration'),
        'rating': rating if rating not in (None, '') else None,
        'director': data.get('director'),
        'actors': data.get('cast'),
        'language': data.get('language'),
        'type': data.get('filmTypes'),
        'poster_url': data.get('pic'),
        'description': data.get('intro'),
        'release_date': _parse_date(data.get('publishTime')),
        'status': Movie.STATUS_HOT,  # 默认热映，待映由待映接口置为 COMING
    }
    obj, created = Movie.objects.update_or_create(
        up_movie_id=movie_id, defaults=defaults,
    )
    return obj


def upsert_cinema_from_data(data, fallback_city_code=None):
    """影院列表/详情数据入库（字段映射 §3.1/3.2）。

    fallback_city_code：麻花 cinemaList 返回体并不保证带 cityId（文档未列该字段），
    故由调用方（pull_cinemas 已知本次查询的麻花 cityId）传入兜底，避免 city_code 落成 '0'
    导致「按城市过滤」时匹配不到（Cinema.city_code 与前端 cityCode 均用麻花 cityId）。
    """
    cinema_id = str(data.get('cinemaId', ''))
    if not cinema_id:
        return None

    lng = data.get('lng')
    lat = data.get('lat')
    city_code = str(data.get('cityId') or fallback_city_code or '') or '0'
    defaults = {
        'name': data.get('cinemaName', ''),
        'address': data.get('cinemaAddress'),
        'region': data.get('regionName'),
        'brand': data.get('brand'),
        'city_code': city_code,
        'lng': lng if lng not in (None, '') else None,
        'lat': lat if lat not in (None, '') else None,
    }
    obj, created = Cinema.objects.update_or_create(
        up_cinema_id=cinema_id, defaults=defaults,
    )
    return obj


def _upsert_schedule(data):
    """单条排片入库（字段映射 §3.3）。

    麻花 price=原价(挂牌价)，fastPrice=快速出票最低价（真实最低可售口径）；
    end_at 用场次返回的 duration 推算（散场时间），接口未给时留空。
    """
    show_id = data.get('showId')
    if not show_id:
        return None

    # 定位影院/电影（用 up_ 外键）
    cinema = Cinema.objects.filter(up_cinema_id=str(data.get('cinemaId', ''))).first()
    movie = Movie.objects.filter(up_movie_id=str(data.get('filmId', ''))).first()

    price = _parse_price(data.get('price'))
    fast_price = _parse_price(data.get('fastPrice'))
    start = _parse_datetime(data.get('showTime')) or timezone.now()
    # 停售时间：我方放单不传 model，麻花默认 0-特惠模式，故以「特惠停售时间」为准。
    # 兼容报文里可能只给其中一种口径，按 common -> 通用 stopsellTime -> fast 依次兜底；
    # 全都没有时留空，由 sell_deadline() 回退到 start_at。
    stopsell = (_parse_datetime(data.get('stopsellTimeCommon'))
                or _parse_datetime(data.get('stopsellTime'))
                or _parse_datetime(data.get('stopsellTimeFast')))
    try:
        duration_min = int(data.get('duration'))
    except (TypeError, ValueError):
        duration_min = None

    defaults = {
        'cinema_id': cinema.id if cinema else 0,
        'movie_id': movie.id if movie else 0,
        'hall_name': data.get('hallName'),
        'start_at': start,
        'end_at': (start + timedelta(minutes=duration_min)) if (start and duration_min) else None,
        'stopsell_at': stopsell,
        'show_type': data.get('planType') or data.get('showVersionType'),
        'language': data.get('language'),
        'min_price': fast_price if fast_price is not None else (price if price is not None else 0),
        'origin_price': price,
        'snapshot_at': timezone.now(),
    }
    obj, created = Schedule.objects.update_or_create(
        up_schedule_id=show_id, defaults=defaults,
    )
    return obj


def sync_schedule_callback(payload):
    """排片批量回调（字段映射 §6.4）：delList 删除 + updList 更新 + addList 新增。"""
    from django.utils import timezone

    cinema_id = payload.get('cinemaId')

    # 删除
    for item in payload.get('delList') or []:
        show_id = item.get('showId')
        if show_id:
            Schedule.objects.filter(up_schedule_id=show_id).update(deleted=1)

    # 新增
    for item in payload.get('addList') or []:
        _upsert_schedule(item)

    # 更新
    for item in payload.get('updList') or []:
        _upsert_schedule(item)

    return True


# ---------------------------------------------------------------------------
# 实时拉取麻花数据（放单前置链路：影院列表 → 影院排片 → 座位情况）
#
# 设计：
#   - 影院、排片：拉取后 upsert 入库（相对稳，可缓存），前端继续用 DB 主键。
#   - 座位：麻花明确「禁止拉取同步、实时获取、限频 60/min」，故每次实时调用、
#     仅在本函数内即时映射为前端选座结构，绝不落库缓存。
# ---------------------------------------------------------------------------

# 麻花座位状态 → 前端座位状态（0空座 1已售 3不可售）
_MAHUA_SEAT_STATUS = {'N': 0, 'LK': 1, 'E': 3}


def _mahua():
    from apps.upadapter.mahua import MahuaClient
    from apps.upadapter.token import get_token
    return MahuaClient(), get_token()


def pull_cinemas(mahua_city_id, up_movie_id=None, lng=None, lat=None,
                 region_name=None, keywords=None, brand=None,
                 date=None, sort_by=None, hall_keyword=None):
    """实时拉取影院列表并 upsert 入库。

    mahua_city_id：麻花 cityId（成都=8）；up_movie_id：麻花影片ID（=cinemaList 的 filmId）。
    date：上映/观影日期 yyyy-MM-dd。注意——cinemaList 仅在同时传 filmId+date 时才会真正
    按影片过滤影院；只传 filmId 不传 date 会退化成返回整城影院（含未排片），故按影片查询务必带 date。
    sort_by：price 价格升序 / distance 距离升序；hall_keyword：影厅关键词数组。
    返回本次命中的麻花影院数组（rtnData，用于视图进一步过滤/排序）。
    """
    client, token = _mahua()
    filters = {}
    if up_movie_id:
        filters['filmId'] = up_movie_id
    if date:
        filters['date'] = date
    if lng is not None and lat is not None:
        filters['lng'] = lng
        filters['lat'] = lat
    if region_name:
        filters['regionName'] = region_name
    if keywords:
        filters['keywords'] = keywords
    if brand:
        filters['brand'] = brand
    if hall_keyword:
        filters['hallKeyword'] = hall_keyword
    if sort_by:
        filters['sortBy'] = sort_by

    code, data = client.get_cinema_list(token, mahua_city_id, **filters)
    cinemas = data if isinstance(data, list) else (data or {}).get('list') or []
    for item in cinemas:
        upsert_cinema_from_data(item, fallback_city_code=mahua_city_id)
    logger.info('实时拉影院 city=%s film=%s date=%s 命中 %s', mahua_city_id, up_movie_id, date, len(cinemas))
    return cinemas


def pull_regions(mahua_city_id, date=None, up_movie_id=None, ttl=300):
    """实时拉取某城市的影院区域列表（「全城▾」筛选项）。

    mahua_city_id：麻花 cityId（成都=8）。date 选填（不传即全市静态区域）；
    up_movie_id：麻花影片ID，传则收敛为「有该片排片的区」。
    过滤掉 regionName 为空的脏数据，返回 [{'name', 'count'}]，按影院数降序。

    区域列表相对稳定且打开下拉即拉，故加短 TTL 缓存（默认 300s）按
    city+date+film 维度去重，减轻麻花限频压力。缓存层用 Django cache 框架
    （生产 Redis / 本地 LocMem），异常时降级为直连、不影响可用性。
    """
    from django.core.cache import cache
    key = 'mahua:regions:%s:%s:%s' % (mahua_city_id, date or '-', up_movie_id or '-')
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001 - 缓存不可用时降级直连
        pass

    client, token = _mahua()
    code, data = client.get_cinema_regions(token, mahua_city_id, date=date, film_id=up_movie_id)
    rows = data if isinstance(data, list) else (data or {}).get('list') or []
    regions = []
    for r in rows:
        name = r.get('regionName')
        if not name:
            continue  # 过滤 regionName 为 null 的脏数据
        regions.append({'name': name, 'count': r.get('num', 0)})
    regions.sort(key=lambda x: (-x['count'], x['name']))
    logger.info('实时拉区域 city=%s film=%s date=%s 命中 %s', mahua_city_id, up_movie_id, date, len(regions))
    if regions:  # 仅缓存非空结果，避免把偶发空集固化
        try:
            cache.set(key, regions, ttl)
        except Exception:  # noqa: BLE001
            pass
    return regions


def pull_schedules(mahua_cinema_id, up_movie_id=None):
    """实时拉取某影院排片并 upsert 入库。

    mahua_cinema_id：麻花 cinemaId。返回 upsert 后本影院（可限影片）的 Schedule 查询集。
    """
    client, token = _mahua()
    code, data = client.get_schedule(token, mahua_cinema_id)
    rows = data if isinstance(data, list) else (data or {}).get('list') or []
    for item in rows:
        _upsert_schedule(item)

    qs = Schedule.objects.filter(deleted=0)
    cinema = Cinema.objects.filter(up_cinema_id=str(mahua_cinema_id)).first()
    if cinema:
        qs = qs.filter(cinema_id=cinema.id)
    if up_movie_id:
        movie = Movie.objects.filter(up_movie_id=str(up_movie_id)).first()
        if movie:
            qs = qs.filter(movie_id=movie.id)
    logger.info('实时拉排片 cinema=%s 命中 %s', mahua_cinema_id, len(rows))
    return qs.order_by('start_at')


def pull_seats(show_id):
    """实时拉取座位情况（禁缓存，逐次调用）。

    show_id：麻花 showId（=Schedule.up_schedule_id）。
    返回 (rows, restrictions, min_price_fen)：
        rows: [{'row': int, 'seats': [{col,name,seatId,status,price}, ...]}, ...]
        price 单位为分；status 0可售/1已售/3不可售。
    """
    client, token = _mahua()
    code, data = client.get_seats_realtime(token, show_id)
    if not isinstance(data, dict):
        return [], None, None

    seat_data = data.get('movieFilmSeatData') or []
    restrictions = data.get('restrictions')

    grouped = {}
    for s in seat_data:
        try:
            r = int(s.get('rowNo'))
            c = int(s.get('columnNo'))
        except (TypeError, ValueError):
            continue
        price_fen = _parse_price(s.get('price'))
        seat = {
            'col': c,
            'name': s.get('seatNo'),                 # 展示 + 放单 row/col 从此名解析
            'seatId': s.get('seatId'),               # 放单主用此参数
            'status': _MAHUA_SEAT_STATUS.get(s.get('status'), 3),
            'price': price_fen,
            'lovestatus': s.get('lovestatus', 0),
            'area': s.get('area'),
        }
        grouped.setdefault(r, []).append(seat)

    rows = []
    for r in sorted(grouped):
        seats = sorted(grouped[r], key=lambda x: x['col'])
        rows.append({'row': r, 'seats': seats})

    min_price = min((sf['price'] for sr in rows for sf in sr['seats'] if sf['price']), default=None)
    return rows, restrictions, min_price
