"""屏2 · 规则维度参考数据（refdata）——只读。

给运营在配置上浮规则时「查/选」四个 ID 型维度提供数据源，替代手填裸 ID。
口径严格对齐 apps/catalog/markup.py 的维度匹配（见 resolve_markup / resolve_for_schedule）：

  - movie_ids  → 匹配 Schedule.movie_id = **内部 Movie.id（Django 主键）**，不是麻花 up_movie_id。
                 影片搜索返回内部 id + 片名，前端显示片名、写入内部 id。
  - city_codes → 匹配 Cinema.city_code = **麻花城市ID（字符串，如成都=8）**，非国标 std_code。
  - brands     → 匹配 Cinema.brand（院线品牌，自由字符串），取自本地影院表 distinct。
  - hall_types → 匹配 Schedule.show_type（2D/3D/IMAX…），取自排片 distinct；另附常用预设便于主动配置。

全部走 AdminJWTAuthentication + IsAdmin，挂在 /api/v1/admin/ 前缀下，天然纳入：
①CORS_URLS_REGEX（仅 /admin 放开）②JWTAuthMiddleware 对 /admin 的整体豁免（由本处权限自守）。
只读、无副作用，不清规则缓存。
"""
from django.db.models import Count, Q
from rest_framework.decorators import api_view, authentication_classes, permission_classes

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.permissions import IsAdmin
from apps.catalog.models import Cinema, CinemaHall, City, Movie, Schedule
from apps.common.response import ok

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)

_STATUS_LABEL = {1: '热映', 2: '待映', 3: '已下线'}

# 影厅/show_version 常用档位（数据驱动结果之外补充，方便主动预设尚未出现的制式）
_HALL_PRESETS = ['2D', '3D', 'IMAX', 'IMAX3D', '中国巨幕', 'CINITY', '杜比影院', '4DX']

_MAX_LIMIT = 50


def _clamp_limit(raw, default=30):
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = default
    return max(1, min(n, _MAX_LIMIT))


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def refdata_movies(request):
    """影片查找（供影片维度多选）。

    query:
      - kw      : 片名模糊搜索（icontains）。
      - ids     : 逗号分隔的内部 Movie.id 列表，用于「编辑已有规则」时按 ID 反查片名回填。
      - status  : 可选 1/2/3 过滤（默认不过滤；无 kw/ids 时默认给热映供浏览）。
      - limit   : 条数上限，默认 30，最大 50。
    返回 items：[{id(内部PK), name, up_movie_id, status, status_label, release_date, poster_url}]。
    """
    qs = Movie.objects.filter(deleted=0)

    ids_raw = (request.query_params.get('ids') or '').strip()
    kw = (request.query_params.get('kw') or '').strip()
    status = (request.query_params.get('status') or '').strip()
    limit = _clamp_limit(request.query_params.get('limit'))

    if ids_raw:
        id_list = [int(x) for x in ids_raw.split(',') if x.strip().isdigit()]
        qs = qs.filter(id__in=id_list)
    elif kw:
        qs = qs.filter(name__icontains=kw)
        if status in ('1', '2', '3'):
            qs = qs.filter(status=int(status))
    else:
        # 无搜索词时给一份可浏览的默认排序（热映优先、按想看/上映日）
        qs = qs.filter(status=int(status) if status in ('1', '2', '3') else Movie.STATUS_HOT)

    rows = list(qs.order_by('-want_count', '-release_date', 'id')[:limit])
    items = [{
        'id': m.id,
        'name': m.name,
        'up_movie_id': m.up_movie_id,
        'status': m.status,
        'status_label': _STATUS_LABEL.get(m.status, ''),
        'release_date': m.release_date.isoformat() if m.release_date else None,
        'poster_url': m.poster_url,
    } for m in rows]
    return ok({'items': items})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def refdata_cities(request):
    """城市查找（写入 city_codes = 麻花城市ID 字符串）。

    query: kw（城市名/拼音模糊）。无 kw 返回全部（城市量级小，直接给全表）。
    返回 items：[{city_code(麻花ID, 字符串), city_name, pinyin, std_code, hot}]。
    """
    qs = City.objects.all()
    kw = (request.query_params.get('kw') or '').strip()
    if kw:
        qs = qs.filter(Q(city_name__icontains=kw) | Q(pinyin__icontains=kw))
    rows = list(qs.order_by('-hot', 'pinyin'))
    items = [{
        'city_code': c.city_code,
        'city_name': c.city_name,
        'pinyin': c.pinyin,
        'std_code': c.std_code,
        'hot': c.hot,
    } for c in rows]
    return ok({'items': items})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def refdata_brands(request):
    """院线品牌查找（写入 brands = Cinema.brand 字符串）。

    query: city_code（可选，按麻花城市ID 收敛品牌）；kw（品牌名模糊）。
    返回 items：[{name, count}]，count = 该品牌在营影院数，按数量降序。
    """
    qs = Cinema.objects.filter(deleted=0).exclude(brand__isnull=True).exclude(brand='')
    city_code = (request.query_params.get('city_code') or '').strip()
    kw = (request.query_params.get('kw') or '').strip()
    if city_code:
        qs = qs.filter(city_code=city_code)
    if kw:
        qs = qs.filter(brand__icontains=kw)
    rows = (qs.values('brand').annotate(count=Count('id'))
            .order_by('-count', 'brand'))
    items = [{'name': r['brand'], 'count': r['count']} for r in rows]
    return ok({'items': items})


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def refdata_hall_types(request):
    """影厅/show_version 档位查找（写入 hall_types = Schedule.show_type 字符串）。

    数据源：真实排片的 show_type distinct（口径最准）∪ 影厅表 hall_type distinct（更全）。
    返回 items：[{value, count, source}]，source='schedule'|'hall'；另附 presets 常用档位。
    """
    return ok(_hall_types_data())


def _hall_types_data():
    """影厅档位聚合（供 refdata_hall_types / refdata_options 复用，返回裸 dict）。"""
    sched = (Schedule.objects.filter(deleted=0).exclude(show_type__isnull=True)
             .exclude(show_type='').values('show_type').annotate(count=Count('id'))
             .order_by('-count'))
    agg = {}
    order = []
    for r in sched:
        v = r['show_type']
        agg[v] = {'value': v, 'count': r['count'], 'source': 'schedule'}
        order.append(v)
    halls = (CinemaHall.objects.exclude(hall_type__isnull=True).exclude(hall_type='')
             .values('hall_type').annotate(count=Count('id')).order_by('-count'))
    for r in halls:
        v = r['hall_type']
        if v in agg:
            continue
        agg[v] = {'value': v, 'count': r['count'], 'source': 'hall'}
        order.append(v)
    return {'items': [agg[v] for v in order], 'presets': _HALL_PRESETS}


@api_view(['GET'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
def refdata_options(request):
    """枚举型维度一次性引导数据（登录后加载，减少往返）。

    影片量大且需实时搜索，走独立 /refdata/movies，不在此返回。
    返回：{cities:[...], brands:[...], hall_types:[...], presets:[...]}。
    """
    cities = City.objects.all().order_by('-hot', 'pinyin')
    city_items = [{'city_code': c.city_code, 'city_name': c.city_name,
                   'pinyin': c.pinyin, 'hot': c.hot} for c in cities]

    brand_rows = (Cinema.objects.filter(deleted=0).exclude(brand__isnull=True).exclude(brand='')
                  .values('brand').annotate(count=Count('id')).order_by('-count', 'brand'))
    brand_items = [{'name': r['brand'], 'count': r['count']} for r in brand_rows]

    hall = _hall_types_data()
    return ok({
        'cities': city_items,
        'brands': brand_items,
        'hall_types': hall['items'],
        'presets': hall['presets'],
    })
