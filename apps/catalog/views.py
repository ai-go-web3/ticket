"""catalog 视图：城市/影片/影院/排期查询。"""
import hmac
from datetime import timedelta
from math import asin, cos, radians, sin, sqrt

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny

from apps.catalog.models import City, Movie, Cinema, Schedule
from apps.catalog.serializers import (
    CitySerializer, MovieSerializer, CinemaSerializer, ScheduleSerializer, WEEKDAYS,
)
from apps.catalog import seat_map
from apps.catalog import services as catalog_services
from apps.common.response import ok, fail, BizError, ErrorCode


@api_view(['GET'])
@permission_classes([AllowAny])
def cities(request):
    """城市列表（热门优先）。"""
    qs = City.objects.all().order_by('-hot', 'pinyin')
    return ok(CitySerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def movies(request):
    """影片列表。status: 1热映 2待映。

    热映支持 cityCode（前端当前城市，麻花 cityId 或国标码）：实时拉麻花
    movieOnInfoList(ci=城市id)，序列化结果直接进内存缓存（命中零 DB 查询，
    2 小时）；城市解析失败回退 SYNC_DEFAULT_CITY。待映为全国列表（麻花
    comingList 无城市维度），按需最多拉 30 条、缓存 12 小时。
    拉取失败降级用 DB 数据兜底并短缓存 30 分钟。
    """
    status = request.query_params.get('status', '1')
    if status == '1':
        city_code = request.query_params.get('cityCode')
        mahua_city = catalog_services.resolve_city_code(city_code) if city_code else None
        if not mahua_city:
            mahua_city = str(getattr(settings, 'SYNC_DEFAULT_CITY', '8'))
        return ok(catalog_services.get_movies_payload(mahua_city, status='hot'))
    return ok(catalog_services.get_movies_payload(status='coming'))


@api_view(['GET'])
@permission_classes([AllowAny])
def movie_detail(request, movie_id):
    """影片详情。"""
    try:
        movie = Movie.objects.get(id=movie_id, deleted=0)
    except Movie.DoesNotExist:
        raise BizError('影片不存在', code=40400)
    return ok(MovieSerializer(movie).data)


def _haversine(lng1, lat1, lng2, lat2):
    """两点球面距离（米）。"""
    r = 6371000.0
    dlon = radians(lng2 - lng1)
    dlat = radians(lat2 - lat1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(a))


def _mark_no_show(movie, pulled_up_ids, date):
    """麻花权威空/非空结果 → 维护影片「无排片」标记（Movie.no_show_at）。

    闭环节路：麻花热映列表(movieOnInfoList)会挂着当前已无任何场次的长尾影片，
    列表页仍按「特惠购」推荐，用户点进详情却是空列表。这里在按影片查影院的
    权威结果处回写标记：
      - 查的就是今天（显式或默认补齐）且麻花返回空集 → 打标（列表显示「暂无排片」）；
      - 任意日期查到有排片 → 清标（影片恢复可售推荐，不怕误伤复映/晚场影片）。
    """
    if movie is None:
        return
    today = timezone.now().date().isoformat()
    try:
        if pulled_up_ids:
            # 有排片即恢复可售（幂等：未标记时 update 命中 0 行）
            Movie.objects.filter(id=movie.id).exclude(no_show_at=None).update(no_show_at=None)
        elif date == today:
            Movie.objects.filter(id=movie.id, no_show_at=None).update(no_show_at=timezone.now())
    except Exception:  # noqa: BLE001 - 标记失败不影响主流程
        import logging
        logging.getLogger('app').warning('维护影片无排片标记失败 movie=%s', movie.id)


@api_view(['GET'])
@permission_classes([AllowAny])
def cinemas(request):
    """影院列表。支持：城市/影片/区域/关键词/品牌/影厅/日期筛选，按距离或最低价排序。

    query: cityCode, movieId, area, kw, brand, hallKeyword(逗号分隔多个), date(YYYY-MM-DD),
           lng, lat, orderBy(distance|price),
           limit（返回条数上限，默认 30，最大 100——列表页看不了太多，减少传输量）
    返回按 orderBy 排列，distance 为米（未传经纬度时为 null）。

    权威口径：cityCode 查询时，过滤（经纬度/regionName/keywords/brand/hallKeyword/filmId+date）
    与排序（sortBy）全部下推给麻花 cinemaList 完成，直接以麻花返回的列表顺序为准——
    本地仅做「麻花 cinemaId → 本地 Cinema.id」映射补主键、distancekm 为米换算
    （异常/缺失时回退本地 haversine），不再本地重过滤重排序。
    麻花失败才整体回退本地库过滤（旧路径保留兜底）。
    注意：按影片(movieId)查询时，麻花 cinemaList 必须同时带 filmId+date 才会真正按影片过滤，
    否则退化为整城影院（含未排片）；故此处对影片查询默认补 date=今天。
    """
    city_code = request.query_params.get('cityCode')
    movie_id = request.query_params.get('movieId')
    area = request.query_params.get('area')
    kw = request.query_params.get('kw')
    brand = request.query_params.get('brand') or None
    hall_kw_raw = request.query_params.get('hallKeyword')
    hall_keyword = ([k.strip() for k in hall_kw_raw.split(',') if k.strip()]
                    if hall_kw_raw else None)
    date = request.query_params.get('date')  # YYYY-MM-DD
    order_by = request.query_params.get('orderBy') or 'distance'
    try:
        limit = min(max(int(request.query_params.get('limit', 30)), 1), 100)
    except (TypeError, ValueError):
        limit = 30
    try:
        lng = float(request.query_params.get('lng'))
        lat = float(request.query_params.get('lat'))
    except (TypeError, ValueError):
        lng = lat = None

    # 实时从麻花拉取该「影片+城市+日期」的影院并同步入库（失败则回退用库中已有数据）
    up_movie_id = None
    if movie_id:
        mv = Movie.objects.filter(id=movie_id).first()
        up_movie_id = mv.up_movie_id if mv else None
        # 按影片查询但未指定日期时，默认取「今天」，避免拉到未排片的影院
        if up_movie_id and not date:
            date = timezone.now().date().isoformat()
    pulled = None         # 非 None 表示以麻花返回列表为权威（即便为空集=该日期确无排片）
    pull_failed = False
    if city_code:
        try:
            mahua_city = catalog_services.resolve_city_code(city_code)
            sort_by = order_by if order_by in ('distance', 'price') else None
            pulled = catalog_services.pull_cinemas(
                mahua_city, up_movie_id=up_movie_id,
                lng=lng, lat=lat, region_name=area, keywords=kw,
                brand=brand, hall_keyword=hall_keyword,
                date=date, sort_by=sort_by,
            )
            # 麻花按 filmId(+date) 返回的影院，天然即「该日期在映该片的影院」
            if up_movie_id:
                pulled_up_ids = {str(c.get('cinemaId')) for c in pulled if c.get('cinemaId') is not None}
                _mark_no_show(mv, pulled_up_ids, date)
        except Exception as exc:  # noqa: BLE001
            pull_failed = True
            import logging
            logging.getLogger('app').warning('实时拉影院失败，回退库数据: %s', exc)

    if pulled is not None:
        # 麻花权威模式：顺序=麻花排序结果（distance/price），不做本地重排。
        # 本地批量映射主键 + 补齐麻花列表体没有的字段（phone/supports 等）。
        items = _cinemas_from_mahua(pulled, lng=lng, lat=lat)
        return ok(CinemaSerializer(items[:limit], many=True).data)

    # 回退路径（麻花失败或未传 cityCode）：本地库过滤
    qs = Cinema.objects.filter(deleted=0, business_status__in=[1, 3])
    if city_code:
        qs = qs.filter(city_code=city_code)
    if area:
        qs = qs.filter(region=area)
    if kw:
        qs = qs.filter(Q(name__icontains=kw) | Q(address__icontains=kw))
    if movie_id and pull_failed:
        # 仅在「按影片查询且实时拉取失败」时才回退本地排片，避免正常路径下混入未排片影院
        # 可售口径统一走 sellable_schedules()，否则只剩过期场次的影院会被错误列出
        cinema_ids = (catalog_services.sellable_schedules()
                      .filter(movie_id=movie_id)
                      .values_list('cinema_id', flat=True).distinct())
        qs = qs.filter(id__in=cinema_ids)

    cinemas = list(qs)
    # 注入距离
    if lng is not None and lat is not None:
        for c in cinemas:
            if c.lng is not None and c.lat is not None:
                c._distance = round(_haversine(lng, lat, float(c.lng), float(c.lat)))
            else:
                c._distance = None
        if order_by == 'distance':
            cinemas.sort(key=lambda c: (c._distance is None, c._distance or 0))

    return ok(CinemaSerializer(cinemas[:limit], many=True).data)


def _mahua_distance_m(raw, lng, lat):
    """麻花列表项 distancekm（km 字符串）→ 米整数；无效/为 0 时回退本地 haversine。"""
    try:
        km = float(raw.get('distancekm') or 0)
    except (TypeError, ValueError):
        km = 0
    if km > 0:
        return int(round(km * 1000))
    if lng is not None and lat is not None and raw.get('lng') and raw.get('lat'):
        try:
            return round(_haversine(lng, lat, float(raw['lng']), float(raw['lat'])))
        except (TypeError, ValueError):
            return None
    return None


def _cinemas_from_mahua(pulled, lng=None, lat=None):
    """麻花权威列表 → 本地 Cinema 实例序列（保持麻花顺序）。

    逐项映射 up_cinema_id → 本地行（pull_cinemas 已 upsert，正常必然命中；
    未命中的脏数据丢弃），注入 _distance（distancekm 换算，回退 haversine）
    与 _refundable（麻花 refundStatus）。顺序即麻花 sortBy 结果，不重排。
    """
    by_up = {c.up_cinema_id: c for c in Cinema.objects.filter(
        up_cinema_id__in=[str(i.get('cinemaId')) for i in pulled if i.get('cinemaId') is not None],
        deleted=0,
    )}
    items = []
    for raw in pulled:
        up_id = str(raw.get('cinemaId', ''))
        obj = by_up.get(up_id)
        if obj is None:
            continue
        obj._distance = _mahua_distance_m(raw, lng, lat)
        obj._refundable = raw.get('refundStatus')
        items.append(obj)
    return items


@api_view(['GET'])
@permission_classes([AllowAny])
def cinema_areas(request):
    """某城市下的影院区域列表（用于「全城▾」筛选）。

    数据源：麻花「影院区域数量」接口 filterCinemas/region，实时拉取。
    query: cityCode（必填）、date（YYYY-MM-DD，不传默认今天）、movieId（可选，
           传则收敛为「有该片排片的区」，用于影片搜索/详情场景）。
    返回：[{name, count}]，count 为该区影院数，按数量降序。
    口径：按影片筛选（movieId 能解析出有效 up_movie_id）时，以麻花结果为权威——
    麻花返回空即「该影片在本城当日无排片」，直接返回空列表（与 cinemas 接口一致），
    不再回退成整城区域误导上层。仅在「未按影片筛选」且麻花异常/返回空时，
    才回退本地 Cinema 表统计，保证「全城▾」下拉不空。
    """
    city_code = request.query_params.get('cityCode')
    date = request.query_params.get('date') or timezone.now().date().isoformat()
    movie_id = request.query_params.get('movieId')

    up_movie_id = None
    if movie_id:
        mv = Movie.objects.filter(id=movie_id).first()
        up_movie_id = mv.up_movie_id if mv else None

    if city_code:
        try:
            mahua_city = catalog_services.resolve_city_code(city_code)
            regions = catalog_services.pull_regions(
                mahua_city, date=date, up_movie_id=up_movie_id,
            )
            if regions:
                return ok(regions)
            # 按影片筛选时，麻花返回空 = 该片在本城确无排片，空集即权威结果，
            # 直接返回空列表（与 cinemas 口径一致），不回退成整城区域误导上层。
            if up_movie_id:
                return ok([])
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger('app').warning('实时拉区域失败，回退库数据: %s', exc)

    # 回退（仅未按影片筛选、或麻花异常时）：本地 Cinema 表按 region 统计，保证「全城▾」不空
    qs = Cinema.objects.filter(deleted=0, business_status__in=[1, 3])
    if city_code:
        qs = qs.filter(city_code=city_code)
    rows = (qs.exclude(region__isnull=True).exclude(region='')
            .values('region').annotate(count=Count('id')).order_by('-count', 'region'))
    return ok([{'name': r['region'], 'count': r['count']} for r in rows])


@api_view(['GET'])
@permission_classes([AllowAny])
def cinema_brands(request):
    """某城市下的影院品牌列表（用于「品牌▾」筛选）。

    数据源：麻花「影院品牌数量」接口 filterCinemas/brand，实时拉取（300s 短缓存）。
    query: cityCode（必填）、date（YYYY-MM-DD，不传默认今天）、movieId（可选，
           传则收敛为「有该片排片的品牌」）。
    返回：[{name, count}]，count 为该品牌影院数，按数量降序。
    口径与 cinema_areas 一致：按影片筛选（movieId 能解析出有效 up_movie_id）时，
    以麻花结果为权威——麻花返回空即「该影片在本城当日无排片品牌」，返回空列表；
    仅在「未按影片筛选」且麻花异常/返回空时，回退本地 Cinema.brand 统计。
    """
    city_code = request.query_params.get('cityCode')
    date = request.query_params.get('date') or timezone.now().date().isoformat()
    movie_id = request.query_params.get('movieId')

    up_movie_id = None
    if movie_id:
        mv = Movie.objects.filter(id=movie_id).first()
        up_movie_id = mv.up_movie_id if mv else None

    if city_code:
        try:
            mahua_city = catalog_services.resolve_city_code(city_code)
            brands = catalog_services.pull_brands(
                mahua_city, date=date, up_movie_id=up_movie_id,
            )
            if brands:
                return ok(brands)
            if up_movie_id:
                return ok([])
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger('app').warning('实时拉品牌失败，回退库数据: %s', exc)

    # 回退（仅未按影片筛选、或麻花异常时）：本地 Cinema 表按 brand 统计
    qs = Cinema.objects.filter(deleted=0, business_status__in=[1, 3])
    if city_code:
        qs = qs.filter(city_code=city_code)
    rows = (qs.exclude(brand__isnull=True).exclude(brand='')
            .values('brand').annotate(count=Count('id')).order_by('-count', 'brand'))
    return ok([{'name': r['brand'], 'count': r['count']} for r in rows])


@api_view(['GET'])
@permission_classes([AllowAny])
def schedules(request):
    """排期列表（聚合响应）。按影院+影片+日期。

    响应为 {cinema, movies, schedules}：影院详情页一次请求即可渲染整页
    （影院卡 + 影片海报条 + 场次列表），不再补拉 movies/cinemas 两个接口。
    影片/影院信息取自本地库（同步入库数据），join 零成本、不多打麻花；
    movies 仅含本场次有排片的影片（热映+待映，覆盖点映/预售场）。
    """
    cinema_id = request.query_params.get('cinemaId')
    movie_id = request.query_params.get('movieId')
    date = request.query_params.get('date')  # YYYY-MM-DD

    # 实时从麻花拉该影院排片（含真实 showId）并同步入库；失败回退库数据
    up_movie_id = None
    if movie_id:
        mv = Movie.objects.filter(id=movie_id).first()
        up_movie_id = mv.up_movie_id if mv else None
    cinema = None
    if cinema_id:
        cinema = Cinema.objects.filter(id=cinema_id, deleted=0).first()
        if cinema:
            try:
                catalog_services.pull_schedules(cinema.up_cinema_id, up_movie_id=up_movie_id)
            except Exception as exc:  # noqa: BLE001
                import logging
                logging.getLogger('app').warning('实时拉排片失败，回退库数据: %s', exc)

    # 可售口径统一走 sellable_schedules()：未删 + 在售 + 未过停售线
    # （停售线 = 麻花特惠 stopsell_at，缺失时回退 start_at），
    # 避免「22:02 还在卖 22:00 的票」。
    qs = catalog_services.sellable_schedules()
    if cinema_id:
        qs = qs.filter(cinema_id=cinema_id)
    if movie_id:
        qs = qs.filter(movie_id=movie_id)
    if date:
        qs = qs.filter(start_at__date=date)
    qs = qs.order_by('start_at')

    rows = ScheduleSerializer(qs, many=True).data
    movies = MovieSerializer(
        Movie.objects.filter(id__in={r['movie_id'] for r in rows}, deleted=0), many=True,
    ).data
    return ok({
        'cinema': CinemaSerializer(cinema).data if cinema else None,
        'movies': movies,
        'schedules': rows,
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def schedule_seats(request, schedule_id):
    """场次座位图：实时取麻花真实座位（含 seatId）+ 每座价格。

    麻花座位「禁止拉取同步、实时获取」，故每次调用实时拉取；麻花异常时回退
    seat_map 生成的兜底图（保证选座页不空）。返回体带 showId（麻花场次ID）与
    restrictions（最多可选座数），供前端把真实 showId/seatId 带入下单、放单。
    """
    try:
        schedule = Schedule.objects.get(id=schedule_id, deleted=0)
    except Schedule.DoesNotExist:
        raise BizError('场次不存在', code=40400)
    # 已过停售线的场次不进选座页（与列表/锁座/建单同一口径）
    catalog_services.ensure_sellable(schedule)

    show_id = schedule.up_schedule_id
    rows, restrictions, min_price = [], None, None
    try:
        rows, restrictions, min_price = catalog_services.pull_seats(show_id)
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger('app').warning('实时拉座位失败，回退兜底图: %s', exc)

    if rows:
        hall_name = schedule.hall_name or ''
        price = min_price if min_price is not None else (schedule.min_price or 4500)
    else:
        # 兜底：确定性生成（骨架演示/无真实数据时）
        rows, hall_name, price = seat_map.gen_seat_map(schedule)

    return ok({
        'scheduleId': schedule_id,
        'showId': show_id,           # 麻花场次ID（放单 showId）
        'hallName': hall_name,
        'price': price,              # 单位：分
        'restrictions': restrictions,
        'rows': rows,                # [{ row, seats: [{ col, name, seatId, status, price }] }]
    })


@api_view(['GET'])
@permission_classes([AllowAny])
def coming_calendar(request):
    """待映日历：未来 N 天（默认 14）的日期条 + 按上映日分组的影片。

    query: days（默认 14），city_code（预留）
    返回：{ days: [{date, weekday, week_short, day, count}], groups: [{date, weekday, movies}] }
    """
    try:
        days = int(request.query_params.get('days', 14))
    except (TypeError, ValueError):
        days = 14
    days = max(1, min(days, 60))

    start = timezone.now().date()
    end = start + timedelta(days=days - 1)

    movies = list(
        Movie.objects.filter(
            deleted=0, status=Movie.STATUS_COMING,
            release_date__gte=start, release_date__lte=end,
        ).order_by('release_date', '-want_count')
    )

    # 按上映日分组
    grouped = {}
    for m in movies:
        grouped.setdefault(m.release_date, []).append(m)

    day_list = []
    group_list = []
    for i in range(days):
        d = start + timedelta(days=i)
        items = grouped.get(d, [])
        day_list.append({
            'date': d.isoformat(),
            'weekday': WEEKDAYS[d.weekday()],
            'week_short': WEEKDAYS[d.weekday()][1],
            'day': d.day,
            'count': len(items),
        })
        if items:
            group_list.append({
                'date': d.isoformat(),
                'weekday': WEEKDAYS[d.weekday()],
                'movies': MovieSerializer(items, many=True).data,
            })

    return ok({'days': day_list, 'groups': group_list})


@api_view(['POST'])
@permission_classes([AllowAny])
def sync_movies(request):
    """运维：手动/外部触发热映+待映影片同步（也可用于本地联调）。

    与内置定时器共用 catalog_services.run_movie_sync（含进程内缓存锁去重）。

    鉴权（fail-closed）：
      - 服务端未配置 TASK_TOKEN 时一律拒绝。
      - 调用方需携带与 TASK_TOKEN 一致的令牌，取值顺序：
        header `X-Task-Token` > query `?token=` > body(JSON/form) `token`。

    body（可选）：{ "city": "8", "pages": 1 }
    返回：{ code, msg, data: {ran, skipped, city, pages, log} }
    """
    expected = getattr(settings, 'TASK_TOKEN', '') or ''
    if not expected:
        return fail('服务端未配置 TASK_TOKEN，同步接口已禁用', code=ErrorCode.FORBIDDEN)

    provided = (
        request.headers.get('X-Task-Token')
        or request.query_params.get('token')
        or (request.data.get('token') if hasattr(request, 'data') and isinstance(request.data, dict) else None)
        or ''
    )
    if not hmac.compare_digest(str(provided), str(expected)):
        return fail('令牌无效', code=ErrorCode.UNAUTHORIZED)

    body = request.data if isinstance(request.data, dict) else {}
    res = catalog_services.run_movie_sync(city=body.get('city'), pages=body.get('pages', 1))
    if res.get('skipped'):
        return ok({**res, 'reason': 'already_running'}, msg='已有同步任务在执行，本次跳过')
    if res.get('error'):
        return fail(res['error'], code=ErrorCode.UP_ERROR)
    return ok(res, msg='同步完成')
