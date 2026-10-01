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
    """影片列表。status: 1热映 2待映；可选 city_code。"""
    status = request.query_params.get('status', '1')
    qs = Movie.objects.filter(deleted=0, status=status)
    if status == '1':
        qs = qs.order_by('-release_date')
    else:
        qs = qs.order_by('release_date')
    return ok(MovieSerializer(qs, many=True).data)


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


@api_view(['GET'])
@permission_classes([AllowAny])
def cinemas(request):
    """影院列表。支持：城市/影片/区域/关键词/日期筛选，按距离或最低价排序。

    query: cityCode, movieId, area, kw, date(YYYY-MM-DD), lng, lat, orderBy(distance|price)
    返回按距离升序排列，distance 为米（未传经纬度时为 null）。
    注意：按影片(movieId)查询时，麻花 cinemaList 必须同时带 filmId+date 才会真正按影片过滤，
    否则退化为整城影院（含未排片）；故此处对影片查询默认补 date=今天。
    """
    city_code = request.query_params.get('cityCode')
    movie_id = request.query_params.get('movieId')
    area = request.query_params.get('area')
    kw = request.query_params.get('kw')
    date = request.query_params.get('date')  # YYYY-MM-DD
    order_by = request.query_params.get('orderBy') or 'distance'
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
    pulled_up_ids = None   # 非 None 表示以麻花结果为权威（即便为空集=该日期确无排片）
    pull_failed = False
    if city_code:
        try:
            mahua_city = catalog_services.resolve_city_code(city_code)
            sort_by = order_by if order_by in ('distance', 'price') else None
            pulled = catalog_services.pull_cinemas(
                mahua_city, up_movie_id=up_movie_id,
                lng=lng, lat=lat, region_name=area, keywords=kw,
                date=date, sort_by=sort_by,
            )
            # 麻花按 filmId(+date) 返回的影院，天然即「该日期在映该片的影院」，据此约束 DB 结果，
            # 避免依赖尚未同步的本地排片表（否则会把结果误过滤为空）。
            if up_movie_id:
                pulled_up_ids = {str(c.get('cinemaId')) for c in pulled if c.get('cinemaId') is not None}
        except Exception as exc:  # noqa: BLE001
            pull_failed = True
            import logging
            logging.getLogger('app').warning('实时拉影院失败，回退库数据: %s', exc)

    qs = Cinema.objects.filter(deleted=0, business_status__in=[1, 3])
    if city_code:
        qs = qs.filter(city_code=city_code)
    if area:
        qs = qs.filter(region=area)
    if kw:
        qs = qs.filter(Q(name__icontains=kw) | Q(address__icontains=kw))
    if pulled_up_ids is not None:
        qs = qs.filter(up_cinema_id__in=pulled_up_ids)
    elif movie_id and pull_failed:
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

    return ok(CinemaSerializer(cinemas, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def cinema_areas(request):
    """某城市下的影院区域列表（用于「全城▾」筛选）。

    数据源：麻花「影院区域数量」接口 filterCinemas/region，实时拉取。
    query: cityCode（必填）、date（YYYY-MM-DD，不传默认今天）、movieId（可选，
           传则收敛为「有该片排片的区」，用于影片搜索/详情场景）。
    返回：[{name, count}]，count 为该区影院数，按数量降序。
    麻花异常时回退本地 Cinema 表统计，保证下拉不空。
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
        except Exception as exc:  # noqa: BLE001
            import logging
            logging.getLogger('app').warning('实时拉区域失败，回退库数据: %s', exc)

    # 回退：本地 Cinema 表按 region 统计
    qs = Cinema.objects.filter(deleted=0, business_status__in=[1, 3])
    if city_code:
        qs = qs.filter(city_code=city_code)
    rows = (qs.exclude(region__isnull=True).exclude(region='')
            .values('region').annotate(count=Count('id')).order_by('-count', 'region'))
    return ok([{'name': r['region'], 'count': r['count']} for r in rows])


@api_view(['GET'])
@permission_classes([AllowAny])
def schedules(request):
    """排期列表。按影院+影片+日期。"""
    cinema_id = request.query_params.get('cinemaId')
    movie_id = request.query_params.get('movieId')
    date = request.query_params.get('date')  # YYYY-MM-DD

    # 实时从麻花拉该影院排片（含真实 showId）并同步入库；失败回退库数据
    up_movie_id = None
    if movie_id:
        mv = Movie.objects.filter(id=movie_id).first()
        up_movie_id = mv.up_movie_id if mv else None
    if cinema_id:
        cinema = Cinema.objects.filter(id=cinema_id).first()
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

    return ok(ScheduleSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def schedule_seats(request, schedule_id):
    """场次座位图：实时取麻花真实座位（含 seatId）+ 每座价格，叠加本地锁定。

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

    # 叠加「已锁」状态（他人/本场次进行中的锁）
    from apps.seat.models import SeatLockItem
    locked = set(
        SeatLockItem.objects.filter(schedule_id=schedule_id)
        .values_list('seat_no', flat=True)
    )
    for r in rows:
        for s in r['seats']:
            if s['name'] in locked and s['status'] == 0:
                s['status'] = 4  # 4 = 锁定（不可选）

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
