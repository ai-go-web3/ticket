"""首页「本月推荐」推荐位：C 端读取装配 + 后台预览共用逻辑。

设计要点（与项目无外键 / 软删 / 麻花城市ID 口径一致）：
- RecommendSlot.city_code 存的是**麻花城市ID**（与 MarkupRule.city_codes 同口径），
  'all' 或空 = 全国投放。C 端传入的 cityCode（可能是国标码）先经 resolve_city_code
  归一为麻花 cityId 再匹配。
- 生效条件：enabled=1 且未软删 且落在 [effective_from, effective_to] 时间窗内。
- 展示顺序：sort 升序、id 升序；上限 section.max_show 条。
- 空位回退：无任何启用推荐位且 section.fallback_top4=1 时，用热映想看榜 Top4 自动兜底
  （沿用 services._serialized_movies('hot') 的既有排序，零额外口径实现）。
- 影片位（movie）海报/片名/角标按内部 Movie 实时派生，不冗余入库；横幅位（banner）用自身字段。

后台 /admin/recommend/preview 与本模块 C 端视图共用 build_home_recommends()，
保证「运营在后台看到的预览」与「线上真机拿到的」是同一份装配结果。
"""
from django.db.models import Q
from django.utils import timezone

from apps.catalog.models import Movie, RecommendSection, RecommendSlot


def _movie_slide(slot, movie):
    """配置型影片位 → C 端 slide（海报/片名/角标由 Movie 实时派生）。"""
    from apps.catalog.serializers import MovieSerializer
    payload = MovieSerializer(movie).data
    return {
        'type': 'movie',
        'slot_id': slot.id,
        'movie_id': movie.id,
        'name': movie.name,
        'poster_url': movie.poster_url or '',
        'badge': slot.badge or payload.get('buy_tag') or '',
        'badge_color': slot.badge_color or 'pink',
        'buy_tag': payload.get('buy_tag') or '',
        'buy_tag_type': payload.get('buy_tag_type') or 'pink',
        'link': {'type': 'movie', 'ref': str(movie.id)},
    }


def _banner_slide(slot):
    """横幅位 → C 端 slide。"""
    link_type = slot.link_type or RecommendSlot.LINK_NONE
    ref = slot.link_ref or ''
    if link_type == RecommendSlot.LINK_MOVIE and not ref and slot.movie_id:
        ref = str(slot.movie_id)
    return {
        'type': 'banner',
        'slot_id': slot.id,
        'movie_id': slot.movie_id,
        'title': slot.title or '',
        'subtitle': slot.subtitle or '',
        'cta': slot.cta or '',
        'bg': slot.bg or '',
        'image_url': slot.image_url or '',
        'badge': slot.badge or '',
        'badge_color': slot.badge_color or 'pink',
        'link': {'type': link_type, 'ref': ref},
    }


def _fallback_slides(limit):
    """空位回退：热映想看榜 Top N（沿用既有排序，no_show 沉底由 _movies_queryset 保证）。"""
    from apps.catalog.services import _serialized_movies
    rows = _serialized_movies('hot')[:limit]
    slides = []
    for m in rows:
        slides.append({
            'type': 'movie',
            'slot_id': None,          # 自动兜底位，非运营配置
            'movie_id': m['id'],
            'name': m['name'],
            'poster_url': m.get('poster_url') or '',
            'badge': m.get('buy_tag') or '',
            'badge_color': {'blue': 'blue', 'orange': 'gold'}.get(m.get('buy_tag_type'), 'pink'),
            'buy_tag': m.get('buy_tag') or '',
            'buy_tag_type': m.get('buy_tag_type') or 'pink',
            'link': {'type': 'movie', 'ref': str(m['id'])},
        })
    return slides


def build_home_recommends(city_code=None):
    """装配首页推荐位数据（C 端 / 后台预览共用）。

    返回 {section:{title,max_show,fallback_top4}, slides:[...]}。
    """
    from apps.catalog.services import resolve_city_code

    section = RecommendSection.load()
    limit = max(0, int(section.max_show or 0))
    now = timezone.now()

    slides = []
    if limit:
        qs = RecommendSlot.objects.filter(deleted=0, enabled=1)
        # 生效时间窗
        qs = qs.filter(Q(effective_from__isnull=True) | Q(effective_from__lte=now),
                       Q(effective_to__isnull=True) | Q(effective_to__gte=now))
        # 城市匹配：全国（all/空）或等于归一后的麻花城市ID
        if city_code:
            mahua = str(resolve_city_code(city_code))
            qs = qs.filter(Q(city_code__in=('all', '')) | Q(city_code__isnull=True)
                           | Q(city_code=mahua))
        slots = list(qs.order_by('sort', 'id')[:limit])

        # 批量取影片（配置型 movie 位）避免逐条查询
        movie_ids = [s.movie_id for s in slots
                     if s.slot_type == RecommendSlot.TYPE_MOVIE and s.movie_id]
        movies = {}
        if movie_ids:
            for m in Movie.objects.filter(id__in=movie_ids, deleted=0):
                movies[m.id] = m

        for s in slots:
            if s.slot_type == RecommendSlot.TYPE_MOVIE:
                movie = movies.get(s.movie_id)
                if movie:                     # 影片已下线则跳过该位（不占坑）
                    slides.append(_movie_slide(s, movie))
            else:
                slides.append(_banner_slide(s))

    # 空位回退自动 Top4
    if not slides and section.fallback_top4:
        slides = _fallback_slides(4)

    return {
        'section': {
            'title': section.title,
            'max_show': section.max_show,
            'fallback_top4': bool(section.fallback_top4),
        },
        'slides': slides,
    }
