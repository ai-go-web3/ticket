"""上浮规则解析器（定价中枢，被 catalog 展示链路与 order 建单链路共用）。

设计要点：
- 兜底 = 行为不变：无任何规则命中时，沿用 settings.PRICE_MARKUP_RATE（比例上浮），
  与改造前完全等价，保证存量与新增计价一致。
- 维度匹配：movie_ids / brands / city_codes / hall_types / weekday_in / hour 窗口 /
  生效时间；某维度为空即「不限」，规则按 priority 升序、命中即停。
- 加价方式：rate=比例（sale=cost×(1+rate)）；flat=固定（sale=cost+flat_fen/张）。
- 缓存：规则表小、变更少，读缓存 30s TTL；后台保存规则时显式清缓存即时生效。
- 刻意不含 buy_mode 维度：座位 fast/maxSpeed 两档在取价时同一费率，若按购票模式分档
  会造成「展示价 ≠ 下单价」的错位，留待后续（详见 docs/B端运营后台落地方案.md §4）。
"""
from datetime import datetime

from django.conf import settings
from django.core.cache import cache

RULES_CACHE_KEY = 'markup:rules'
RULES_CACHE_TTL = 30  # 秒

MODE_RATE = 'rate'
MODE_FLAT = 'flat'


def _fallback():
    """无命中时的兜底：全局比例费率（= 改造前行为）。"""
    return (MODE_RATE, float(getattr(settings, 'PRICE_MARKUP_RATE', 0.05) or 0), 0)


def _load_rules():
    """加载启用规则（含兜底规则行），按 priority 升序，缓存 30s。"""
    rules = cache.get(RULES_CACHE_KEY)
    if rules is None:
        from apps.catalog.models import MarkupRule
        rules = list(
            MarkupRule.objects.filter(is_active=1)
            .order_by('priority', 'id').values(
                'id', 'priority', 'is_fallback', 'mode', 'rate', 'flat_fen',
                'movie_ids', 'brands', 'city_codes', 'hall_types',
                'weekday_in', 'hour_from', 'hour_to',
                'effective_from', 'effective_to',
            )
        )
        cache.set(RULES_CACHE_KEY, rules, RULES_CACHE_TTL)
    return rules


def clear_rules_cache():
    cache.delete(RULES_CACHE_KEY)


def _dim_hit(dim_list, value):
    """维度命中：规则该维度为空/未设 = 不限（命中）；否则 value 需落在列表内。"""
    if not dim_list:
        return True
    if value is None:
        return False
    return value in dim_list


def _time_hit(rule, show_at):
    """时段命中：weekday_in / [hour_from,hour_to] / 生效起止。未设即不限。"""
    if show_at is None:
        # 无场次时间上下文时，含时段约束的规则不参与匹配（更保守）
        if rule['weekday_in'] or rule['hour_from'] is not None or rule['hour_to'] is not None:
            return False
    else:
        if rule['weekday_in']:
            if show_at.isoweekday() not in rule['weekday_in']:
                return False
        hh = show_at.hour
        if rule['hour_from'] is not None and hh < rule['hour_from']:
            return False
        if rule['hour_to'] is not None and hh > rule['hour_to']:
            return False
    # 生效时间窗
    now = datetime.now()
    if rule['effective_from'] and now < rule['effective_from']:
        return False
    if rule['effective_to'] and now > rule['effective_to']:
        return False
    return True


def resolve_markup(movie_id=None, brand=None, city_code=None, hall_type=None, show_at=None):
    """返回 (mode, rate: float, flat_fen: int)。无命中走全局兜底。

    入参为一次定价的上下文（影片PK / 影院品牌 / 城市码 / 影厅类型或show_type / 开场时间）。
    """
    fallback_row = None
    for rule in _load_rules():
        if rule['is_fallback']:
            fallback_row = rule
            continue
        if not _dim_hit(rule['movie_ids'], movie_id):
            continue
        if not _dim_hit(rule['brands'], brand):
            continue
        if not _dim_hit(rule['city_codes'], city_code):
            continue
        if not _dim_hit(rule['hall_types'], hall_type):
            continue
        if not _time_hit(rule, show_at):
            continue
        return _to_tuple(rule)
    if fallback_row:
        return _to_tuple(fallback_row)
    return _fallback()


def _to_tuple(rule):
    mode = rule['mode'] or MODE_RATE
    rate = float(rule['rate']) if rule['rate'] is not None else 0.0
    flat = int(rule['flat_fen'] or 0) if mode == MODE_FLAT else 0
    return (mode, rate, flat)


def uplift_fen(cost_fen, mode, rate, flat_fen):
    """成本价(分) 按命中规则上浮 -> 售价(分)。cost<=0 原样返回。"""
    if not cost_fen or cost_fen <= 0:
        return cost_fen
    if mode == MODE_FLAT:
        return int(cost_fen) + int(flat_fen)
    return int(round(cost_fen * (1 + rate)))


def reverse_cost_fen(sale_fen, mode, rate, flat_fen):
    """上浮后售价(分) 反推原始成本(分)。flat: sale-flat；rate: sale/(1+rate)。"""
    if not sale_fen or sale_fen <= 0:
        return None
    if mode == MODE_FLAT:
        return max(int(sale_fen) - int(flat_fen), 0)
    return int(round(sale_fen / (1 + rate))) if rate > 0 else int(sale_fen)


def resolve_for_schedule(schedule):
    """便捷：由 Schedule 解析定价上下文（含影院品牌/城市、影片、影厅类型、开场时间）。"""
    if not schedule:
        return _fallback()
    from apps.catalog.models import Cinema
    cinema = Cinema.objects.filter(id=schedule.cinema_id).first()
    return resolve_markup(
        movie_id=schedule.movie_id,
        brand=cinema.brand if cinema else None,
        city_code=cinema.city_code if cinema else None,
        hall_type=schedule.show_type,
        show_at=schedule.start_at,
    )
