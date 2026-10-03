"""catalog 数据同步服务：麻花回调增量入库（电影 / 排片 / 城市）+ 影片按需拉取。"""
import io
import logging
from datetime import date, datetime, timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.db.models import F
from django.db.models.functions import Coalesce
from django.utils import timezone

from apps.catalog.models import City, Movie, Cinema, Schedule
from apps.catalog.city_mapping import (
    MAHUA_CITY_MAP, HOT_CITY_IDS, mahua_to_std,
)

logger = logging.getLogger('app')

# 影片同步并发锁：内置定时器与运维 HTTP 接口共用，保证同一时刻只真正执行一次
SYNC_LOCK_KEY = 'task:sync_movies_lock'
SYNC_LOCK_TTL = 300  # 秒；异常卡死也会自动释放


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


def _markup_fen(fen):
    """成本价(分)按 settings.PRICE_MARKUP_RATE 上浮，四舍五入到分。

    麻花 fastPrice/maxSpeedPrice 是我方成本价，展示与计价前统一先上浮
    （默认 5%），原价 price 为挂牌价不上浮。费率设 0 即不上浮。
    """
    if not fen or fen <= 0:
        return fen
    rate = float(getattr(settings, 'PRICE_MARKUP_RATE', 0.05) or 0)
    return int(round(fen * (1 + rate)))


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
    min_price 存「上浮后优惠售价」（fast 优先 → maxSpeed → 原价），原价存 origin_price；
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
    max_speed_price = _parse_price(data.get('maxSpeedPrice'))
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
        # min_price=优惠售价（分）：fastPrice 优先，其次 maxSpeedPrice，最后原价兜底；
        # fast/maxSpeed 为成本价，先按 PRICE_MARKUP_RATE 上浮再入库，与座位口径一致
        'min_price': _seat_sale_price(
            price, _markup_fen(fast_price), _markup_fen(max_speed_price)) or 0,
        'origin_price': price,
        # 原始价快照（未上浮，分）：对账/审计用，min_price 的上浮前口径
        'raw_price_json': {
            'price': price,
            'fastPrice': fast_price,
            'maxSpeedPrice': max_speed_price,
        },
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
    （进程内 LocMem），异常时降级为直连、不影响可用性。
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


# ---------------------------------------------------------------------------
# 影片按需拉取（读写穿透）：热映 movieOnInfoList(ci=城市)，待映 comingList(全国分页)。
# 缓存 TTL 内直接读本地库；麻花失败降级返回本地已有数据。替代原每日定时同步。
# ---------------------------------------------------------------------------

MOVIES_CACHE_TTL = 7200     # 热映列表缓存 2 小时
COMING_CACHE_TTL = 43200    # 待映列表缓存 12 小时
DEGRADED_CACHE_TTL = 1800   # 麻花拉取失败降级读 DB 的短缓存 30 分钟（故障期不再每请求打麻花）
COMING_LIST_MAX_ITEMS = 30  # 列表接口按需拉取待映条数上限（comingList 每页 10 条）
COMING_PULL_MAX_ITEMS = 100  # 定时全量拉取待映条数上限


def _run_async(fn, *args):
    """DB 维护类写操作（下线/清理）异步执行：不阻塞列表请求，失败仅记日志。

    用短命守护线程实现（无 Celery 等重型依赖）；线程内用独立 DB 连接，
    结束时显式关闭，避免连接泄漏。
    """
    import threading

    def _wrap():
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001 - 后台任务失败不影响主流程
            logger.warning('异步任务 %s 失败: %s', getattr(fn, '__name__', fn), exc)
        finally:
            from django.db import connections
            connections.close_all()

    threading.Thread(target=_wrap, daemon=True,
                     name=f'async-{getattr(fn, "__name__", "task")}').start()


def unwrap_mahua_list(code, data):
    """麻花 (rtnCode, rtnData) → 影片数组；异常返回空列表。"""
    if code != '000000' or not data:
        return []
    if isinstance(data, str):
        import json
        data = json.loads(data)
    return data if isinstance(data, list) else []


def _parse_release(value):
    """麻花 publishTime '2026-10-01 00:00:00' → date，失败 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


def upsert_movies(items, status):
    """麻花影片数组 → movie 表 upsert（热映/待映共用），返回 (created, updated)。

    待映入库跳过已上映（release_date < 今天）的影片：麻花 comingList 会短暂
    混入刚上映的片子，热映列表（movieOnInfoList）才是在映权威，跳过可避免
    把已转热映的影片改回待映。
    """
    today = date.today()
    created = updated = skipped = 0
    for item in items:
        mid = str(item.get('id', ''))
        if not mid:
            continue
        grade = item.get('grade')
        release = _parse_release(item.get('publishTime'))
        if status == Movie.STATUS_COMING and release and release < today:
            skipped += 1
            continue
        wish = item.get('wishNum')
        defaults = {
            'name': item.get('name', ''),
            'status': status,
            'type': item.get('filmTypes'),
            'duration': item.get('duration'),
            'rating': grade if grade not in (None, '') else None,
            'director': item.get('director') or None,
            'actors': item.get('cast') or None,
            'language': item.get('language') or None,
            'poster_url': item.get('pic') or None,
            'description': item.get('intro') or None,
            'release_date': release,
            'want_count': int(wish) if wish not in (None, '') else 0,
            'presale': 1 if (release and release > today) else 0,
            'deleted': 0,
        }
        _, is_new = Movie.objects.update_or_create(up_movie_id=mid, defaults=defaults)
        if is_new:
            created += 1
        else:
            updated += 1
    if skipped:
        logger.info('影片入库跳过已上映待映 %s 部', skipped)
    return created, updated


def cleanup_stale_coming():
    """把已上映（release_date < 今天）却仍标待映的影片置为已下线，统一待映口径。

    麻花把影片移出 comingList 有延迟，仅靠入库跳过清不掉历史数据，这里兜底。
    返回清理数量。
    """
    today = date.today()
    n = Movie.objects.filter(
        deleted=0, status=Movie.STATUS_COMING, release_date__lt=today,
    ).update(status=Movie.STATUS_OFFLINE)
    if n:
        logger.info('清理已上映仍标待映影片 %s 部 → 已下线', n)
    return n


def _movies_queryset(status):
    """影片列表查询集（已按展示规则排序）。热映：想看倒序+上映日期正序；
    已确认无排片的影片沉底（仍展示、置灰引导，不直接隐藏）。"""
    if status == 'hot':
        return (Movie.objects.filter(deleted=0, status=Movie.STATUS_HOT)
                .order_by(F('no_show_at').asc(nulls_first=True),
                          '-want_count', 'release_date'))
    return (Movie.objects.filter(deleted=0, status=Movie.STATUS_COMING)
            .order_by('release_date'))


def _retire_stale_hot(pull_started):
    """热映下线兜底：麻花城市热映列表只增不减地 upsert，影片从列表消失后
    本地仍标热映，导致「列表可点、点进去无排片」（如长尾老片）。

    在每次热映拉取成功后调用：把 updated_at 早于本次拉取开始的在映影片置为
    已下线——本次列表包含的影片刚被 upsert 刷新过 updated_at，多城市场景下
    其他城市列表刷新的影片也不会误伤；即便极端时序误下线，该城市下次拉取
    会重新 upsert 回热映，自愈。
    """
    n = Movie.objects.filter(
        deleted=0, status=Movie.STATUS_HOT, updated_at__lt=pull_started,
    ).update(status=Movie.STATUS_OFFLINE)
    if n:
        logger.info('热映下线 %s 部（麻花城市列表已移除）', n)
    return n


def _serialized_movies(status):
    """按展示规则取 DB 影片并序列化为响应数组（数据缓存未命中/降级时构建）。"""
    from apps.catalog.serializers import MovieSerializer
    return MovieSerializer(_movies_queryset(status), many=True).data


def get_movies_payload(mahua_city_id=None, status='hot', ttl=None, max_items=None):
    """影片列表：内存数据缓存优先，未命中拉麻花，失败降级 DB 构建。

    缓存语义（真·数据缓存，命中零 DB 查询）：
    - cache 直接存序列化后的影片数组（list[dict]），命中即返回，不碰 DB；
    - 拉取成功：构建负载并缓存 2 小时（热映）/ 12 小时（待映）；
    - 拉取失败：用 DB 现有数据构建负载兜底，只缓存 30 分钟——故障期不逐请求
      打麻花，30 分钟后自动重试恢复。
    - 热映下线维护（_retire_stale_hot）与待映清理（cleanup_stale_coming）
      均异步执行，不阻塞列表请求。

    - 热映：movieOnInfoList(ci=麻花城市id)，城市维度数据，mahua_city_id 必传
      （视图层用 resolve_city_code 归一，解析不出时回退 SYNC_DEFAULT_CITY）。
    - 待映：comingList(pageNum)，麻花该接口无城市维度，全国列表；按需最多拉
      max_items（默认 30）条。
    """
    if status == 'hot':
        ttl = ttl or MOVIES_CACHE_TTL
    else:
        ttl = ttl or COMING_CACHE_TTL
        max_items = max_items or COMING_LIST_MAX_ITEMS
    tag = 'national' if status == 'coming' else str(mahua_city_id)
    key = 'mahua:movies:data:%s:%s' % (tag, status)
    try:
        cached = cache.get(key)
        if cached is not None:
            return cached
    except Exception:  # noqa: BLE001 - 缓存不可用时降级直连
        pass

    pull_started = timezone.now()
    try:
        client, token = _mahua()
        if status == 'hot':
            code, data = client.get_hot_movies(token, mahua_city_id)
            items = unwrap_mahua_list(code, data)
            if not items:
                # 空列表不是合法业务结果（token 失效/限频/接口异常都表现为 rtnData 空）。
                # 必须按失败降级：若当成功放行，后续下线维护会把全部在映影片误杀。
                raise RuntimeError('麻花热映列表为空(rtnCode=%s)' % code)
            created, updated = upsert_movies(items, Movie.STATUS_HOT)
            logger.info('实时拉热映 city=%s 命中 %s（新增 %s 更新 %s）',
                        mahua_city_id, len(items), created, updated)
        else:
            created = updated = pulled = 0
            # comingList 每页 10 条：翻页拉到条数上限或拉空为止（循环上限兜底防死循环）
            for p in range(1, max_items + 1):
                code, data = client.get_coming_movies(token, p)
                items = unwrap_mahua_list(code, data)
                if not items:
                    break
                c, u = upsert_movies(items, Movie.STATUS_COMING)
                created += c
                updated += u
                pulled += len(items)
                if pulled >= max_items:
                    break
            if pulled == 0:
                raise RuntimeError('麻花待映列表为空(rtnCode=%s)' % code)
            logger.info('实时拉待映 上限%s条（新增 %s 更新 %s）', max_items, created, updated)

        # 拉取成功：序列化结果直接进内存缓存（命中路径不再读 DB）
        payload = _serialized_movies(status)
        try:
            cache.set(key, payload, ttl)
        except Exception:  # noqa: BLE001
            pass
        # DB 维护（下线/清理）异步做，不占用请求耗时
        if status == 'hot':
            _run_async(_retire_stale_hot, pull_started)
        else:
            _run_async(cleanup_stale_coming)
        return payload
    except Exception as exc:  # noqa: BLE001 - 麻花失败：降级用 DB 数据兜底
        logger.error('按需拉影片失败 status=%s city=%s：%s（降级返回本地数据，%s 分钟内不再重试）',
                     status, tag, exc, DEGRADED_CACHE_TTL // 60)
        payload = _serialized_movies(status)
        try:
            cache.set(key, payload, DEGRADED_CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
        return payload


def run_coming_pull(max_items=COMING_PULL_MAX_ITEMS):
    """全量拉取待映影片（供定时任务/运维手动触发）。

    comingList 为全国分页数据（每页 10 条、无城市维度），循环翻页直到拉空
    或达到条数上限 max_items（默认 100）；入库复用 upsert_movies（按
    up_movie_id 增量更新）。成功结束后写入待映列表缓存标记（12 小时），
    期间 /catalog/movies?status=2 直接查本地。

    返回：{items, created, updated, stopped_by}；stopped_by='empty' 拉空结束 /
    'cap' 达到条数上限 / 'error' 中途异常。
    """
    created = updated = pulled = 0
    stopped_by = 'cap'
    try:
        client, token = _mahua()
        # 循环上限用 max_items 兜底（每页至少 1 条），防接口异常时死循环
        for p in range(1, max_items + 1):
            code, data = client.get_coming_movies(token, p)
            items = unwrap_mahua_list(code, data)
            if not items:
                # 首页即空不是合法结果（token 失效/接口异常），置为 error：
                # 不写缓存、不触发清理，避免把空数据固化 12 小时
                stopped_by = 'empty' if p > 1 else 'error'
                if p == 1:
                    logger.error('待映全量拉取首页即为空(rtnCode=%s)，疑似 token 失效/接口异常', code)
                break
            c, u = upsert_movies(items, Movie.STATUS_COMING)
            created += c
            updated += u
            pulled += len(items)
            if pulled >= max_items:
                break
    except Exception as exc:  # noqa: BLE001 - 中途失败保留已拉数据，下次定时再补
        logger.error('待映全量拉取中断 page=%s：%s', pulled // 10 + 1, exc)
        return {'items': pulled, 'created': created, 'updated': updated, 'stopped_by': 'error'}

    if stopped_by == 'empty':
        # 全量拉取成功：同步刷新内存数据缓存（列表接口 12 小时内直接回内存）
        try:
            cache.set('mahua:movies:data:national:coming',
                      _serialized_movies(Movie.STATUS_COMING), COMING_CACHE_TTL)
        except Exception:  # noqa: BLE001
            pass
    if stopped_by != 'error':
        cleanup_stale_coming()
    logger.info('待映全量拉取完成：items=%s created=%s updated=%s stopped_by=%s',
                pulled, created, updated, stopped_by)
    return {'items': pulled, 'created': created, 'updated': updated, 'stopped_by': stopped_by}


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


def _seat_sale_price(price_fen, fast_fen, max_speed_fen):
    """优惠出票价(分)：上浮后 fastPrice 优先，缺失/非正则取上浮后 maxSpeedPrice，再兜底原价。

    入参 fast/maxSpeed 应先经 _markup_fen 上浮；price_fen 为挂牌原价不上浮。
    保护：上浮后售价高于原价时按原价卖（售价不高于票面原价）。
    """
    for v in (fast_fen, max_speed_fen):
        if v and v > 0:
            if price_fen and price_fen > 0:
                return min(v, price_fen)
            return v
    return price_fen


def _parse_region_prices(raw):
    """麻花 movieFilmSeatPrices（区域价格表）→ {sectionId: (price, fast, maxSpeed)}（分）。

    值有两种形态：对象 {price, fastPrice, maxSpeedPrice} 或纯数字（仅原价，
    见接口文档示例）。座位级的 fastPrice/maxSpeedPrice 常缺失，需按
    seat.sectionId 回退到该表取价。
    """
    out = {}
    for k, v in (raw or {}).items():
        if isinstance(v, dict):
            out[str(k)] = (
                _parse_price(v.get('price')),
                _parse_price(v.get('fastPrice')),
                _parse_price(v.get('maxSpeedPrice')),
            )
        else:
            out[str(k)] = (_parse_price(v), None, None)
    return out


def pull_seats(show_id):
    """实时拉取座位情况（禁缓存，逐次调用）。

    show_id：麻花 showId（=Schedule.up_schedule_id）。
    返回 (rows, restrictions, min_price_fen)：
        rows: [{'row': int, 'seats': [{col,name,seatId,status,price,salePrice}, ...]}, ...]
        price/salePrice 单位为分；status 0可售/1已售/3不可售。
        price=票面原价（不上浮）；salePrice=优惠售价（上浮后 fastPrice 优先，
        无则上浮后 maxSpeedPrice，再无原价）；fastPrice/maxSpeedPrice 为已上浮值，
        前端确认页双模式计价直接使用，全链路同一口径。

    麻花 movieFilmSeatData 字段 ↔ 我方座位字段（严格一一对应）：
        rowNo     -> row      仅画图分组（文档：勿用作下单，与座位排不一定对应）
        columnNo  -> col      仅画图排序（同上）
        seatNo    -> name     座位名展示；放单 row/col 由此名正则提取
        status    -> status   N->0 可售 / LK->1 已售 / E->3 不可售
        seatId    -> seatId   透传给前端做建单快照；放单不传（易变动，用 row/col）
        price     -> price    票面原价（分），缺失回退区域价
        fastPrice -> fastPrice        上浮后下发（分），缺失回退区域价
        maxSpeedPrice -> maxSpeedPrice 同上
        lovestatus/area -> 同名透出
        sectionId -> （不透出）仅用于 movieFilmSeatPrices 区域取价回退
    兜底图（gen_seat_map）无 seatId 字段，前端 data-id 为空、放单只用 row/col。

    取价口径：座位级 fastPrice/maxSpeedPrice 真实报文常缺失（示例仅返回 price），
    缺失时按 seat.sectionId 回退 movieFilmSeatPrices 区域价格表，再回退默认区域；
    原价同理（座位 price 缺失用区域价）。
    """
    client, token = _mahua()
    code, data = client.get_seats_realtime(token, show_id)
    if not isinstance(data, dict):
        return [], None, None

    seat_data = data.get('movieFilmSeatData') or []
    restrictions = data.get('restrictions')
    region_prices = _parse_region_prices(data.get('movieFilmSeatPrices'))
    default_region = region_prices.get('0') or next(iter(region_prices.values()), None)

    grouped = {}
    for s in seat_data:
        try:
            r = int(s.get('rowNo'))
            c = int(s.get('columnNo'))
        except (TypeError, ValueError):
            continue
        # sectionId/area 实测两字段同值（三套供应商报文均如此），都试一遍防只填其一；
        # 都未命中再回退默认区域（key '0' 优先，否则取第一项）
        region = (region_prices.get(str(s.get('sectionId') or ''))
                  or region_prices.get(str(s.get('area') or ''))
                  or default_region or (None, None, None))
        rp, rfast, rmax = region

        price_fen = _parse_price(s.get('price'))
        if price_fen is None:
            price_fen = rp
        # 成本价先上浮再下发：座位chip、选座合计、建单 salePrice 全链路同一口径。
        # 座位级缺失 → 区域价 → （仍无则保持 None，salePrice 兜底原价）
        fast_fen = _markup_fen(_parse_price(s.get('fastPrice')))
        if fast_fen is None:
            fast_fen = _markup_fen(rfast)
        max_speed_fen = _markup_fen(_parse_price(s.get('maxSpeedPrice')))
        if max_speed_fen is None:
            max_speed_fen = _markup_fen(rmax)
        seat = {
            'col': c,
            'name': s.get('seatNo'),                 # 展示 + 放单 row/col 从此名解析
            'seatNo': s.get('seatNo'),               # 麻花原始座位名（对账/排查用，与 name 同源）
            'seatId': s.get('seatId'),               # 建单快照透传；放单不用（易变动）
            'status': _MAHUA_SEAT_STATUS.get(s.get('status'), 3),
            'price': price_fen,
            'salePrice': _seat_sale_price(price_fen, fast_fen, max_speed_fen),
            'fastPrice': fast_fen,          # 快速出票价（分，已上浮）
            'maxSpeedPrice': max_speed_fen, # 极速/更深优惠价（分，已上浮）
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


def run_movie_sync(city=None, pages=1):
    """执行一次影片同步（热映+待映），带缓存锁防同进程内并发重叠。

    供内置定时器（APScheduler）与运维 HTTP 接口共用。
    锁用 Django cache（进程内 LocMem），同一进程内同一时刻只有一个真正执行；
    多副本间不共享（LocMem 为进程内），建议单副本常驻。

    返回：{ran: bool, skipped: bool, city, pages, log}；异常时附 error。
    """
    city = str(city or getattr(settings, 'SYNC_DEFAULT_CITY', '8'))
    try:
        pages = int(pages)
    except (TypeError, ValueError):
        pages = 1
    pages = max(1, min(pages, 10))

    if not cache.add(SYNC_LOCK_KEY, '1', timeout=SYNC_LOCK_TTL):
        logger.info('sync_movies 跳过：已有同步在执行')
        return {'ran': False, 'skipped': True, 'city': city, 'pages': pages, 'log': ''}

    try:
        out = io.StringIO()
        call_command('sync_movies', city=city, pages=pages, stdout=out, stderr=out)
        log = out.getvalue()
        logger.info('sync_movies 完成 city=%s pages=%s', city, pages)
        return {'ran': True, 'skipped': False, 'city': city, 'pages': pages, 'log': log}
    except Exception as exc:  # noqa: BLE001
        logger.exception('sync_movies 执行失败：%s', exc)
        return {'ran': False, 'skipped': False, 'error': str(exc),
                'city': city, 'pages': pages, 'log': ''}
    finally:
        cache.delete(SYNC_LOCK_KEY)
