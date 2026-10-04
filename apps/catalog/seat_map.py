"""座位图生成（骨架阶段确定性兜底）。

麻花座位数据（含真实已售/价格/情侣座）尚未落库前，用 schedule_id 确定性生成
一张标准影厅座位图，保证选座→锁座→下单端到端可跑通。

后续接入麻花「座位」接口后，本模块替换为：
    row/col/seatId/price/status 从麻花返回，已售状态用麻花数据。

数据结构约定（与前端 seat.js / 锁座入参对齐）：
    row: 排号（int，从 1 起）
    col: 列号（int，从 0 起）
    name: 座位名，如 "7排8座"（锁座/下单以此为准）
    status: 0 空座可售 / 1 已售 / 2 情侣座 / 3 不可售 / 4 锁定中
    price: 该座票价（分）
"""
import hashlib

from django.conf import settings


def _seeded_int(schedule_id, salt):
    """基于 schedule_id 生成稳定的伪随机整数。"""
    h = hashlib.md5(f'{schedule_id}:{salt}'.encode()).hexdigest()
    return int(h[:8], 16)


def _markup_fen(fen):
    """成本价(分)按 PRICE_MARKUP_RATE 上浮（与 catalog.services._markup_fen 同口径）。"""
    if not fen or fen <= 0:
        return fen
    rate = float(getattr(settings, 'PRICE_MARKUP_RATE', 0.05) or 0)
    return int(round(fen * (1 + rate)))


def _schedule_prices(schedule):
    """座位三价（分）：(price 原价, fastPrice 上浮后, maxSpeedPrice 上浮后)。

    麻花快照 raw_price_json={price, fastPrice, maxSpeedPrice}（未上浮成本口径）
    优先，成本价上浮后下发；fastPrice/maxSpeedPrice 缺失（或无快照）时直接使用
    原价——三价同值，即无优惠，不虚构折扣。
    """
    raw = schedule.raw_price_json or {}
    price = raw.get('price') or schedule.min_price or 4500
    fast = _markup_fen(raw['fastPrice']) if raw.get('fastPrice') else price
    max_speed = _markup_fen(raw['maxSpeedPrice']) if raw.get('maxSpeedPrice') else price
    # 上浮后售价不得高于原价，避免「原价-售价」出现反向优惠
    return price, min(fast, price), min(max_speed, price)


def gen_seat_map(schedule):
    """生成场次座位图。

    Returns:
        (rows, hall_name, price)
        rows: [{ 'row': int, 'seats': [{col,name,status,price,fastPrice,maxSpeedPrice}, ...] }]
        三价口径与真实麻花座位接口一致（fastPrice/maxSpeedPrice 为已上浮值，分），
        前端确认页双模式计价两种链路同一口径。
    """
    sid = schedule.id
    price, fast_price, max_speed_price = _schedule_prices(schedule)

    # 影厅规模：8~12 排，每排 8~12 座（由 schedule_id 稳定决定）
    row_count = 8 + _seeded_int(sid, 'row') % 5       # 8~12
    col_count = 8 + _seeded_int(sid, 'col') % 5       # 8~12

    rows = []
    for r in range(1, row_count + 1):
        seats = []
        for c in range(col_count):
            status = _seat_status(sid, r, c)
            name = f'{r}排{c + 1}座'
            seats.append({
                'col': c,
                'name': name,
                'seatNo': name,   # 与真实接口契约一致（兜底图座位名即原始座位名）
                'status': status,
                'price': price,
                'fastPrice': fast_price,          # 快速出票价（分，已上浮）
                'maxSpeedPrice': max_speed_price, # 极速/更深优惠价（分，已上浮）
            })
        rows.append({'row': r, 'seats': seats})

    hall_name = schedule.hall_name or f'{row_count}排{col_count}列影厅'
    return rows, hall_name, price


def _seat_status(schedule_id, row, col):
    """决定单座初始状态（确定性，不随请求变化）。

    0 空座 / 1 已售 / 2 情侣座 / 3 不可售
    """
    n = _seeded_int(schedule_id, f'{row}-{col}')
    v = n % 100
    if v < 12:
        return 1    # 已售
    if v < 16:
        return 3    # 不可售（维修/遮挡）
    return 0
