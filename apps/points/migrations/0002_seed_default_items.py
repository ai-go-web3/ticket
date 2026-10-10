"""种子数据：MVP 首批商城商品（PRD 6.6 · seedData 3 款）。

- ¥10 观影代金券：100 份 · 1000 分（=¥10）· HOT
- ¥5 观影代金券：999 份 · 500 分（=¥5）
- 免费观影票：20 份 · 3900 分（≈¥39）· 库存压到剩 8 份以演示「告急/仅剩 X 份」三态 · NEW

幂等：按 name 去重，重复执行不重复插入。reverse 空操作（数据种子不回滚）。
"""
from django.db import migrations

SEED = [
    {
        'name': '¥10 观影代金券', 'category': 1, 'points_price': 1000, 'origin_price_fen': 1000,
        'stock_total': 100, 'stock': 100, 'daily_limit': 3, 'expire_days': 30, 'badge': 'HOT', 'sort': 0,
        'description': '全场通用观影代金券，下单时自动抵扣 ¥10，不可与其他代金券叠加。',
        'notes': '兑换后不可撤销；自兑换日起 30 天内有效，逾期作废；每人每日最多兑 3 张。',
        'applicable': '全国已接入影院通用（特殊场次/活动场除外，以下单页实际抵扣为准）。',
    },
    {
        'name': '¥5 观影代金券', 'category': 1, 'points_price': 500, 'origin_price_fen': 500,
        'stock_total': 999, 'stock': 999, 'daily_limit': 3, 'expire_days': 30, 'badge': '', 'sort': 1,
        'description': '小额观影代金券，下单抵扣 ¥5，适合低频观影党攒积分小额兑换。',
        'notes': '兑换后不可撤销；30 天有效；每人每日最多兑 3 张。',
        'applicable': '全国已接入影院通用。',
    },
    {
        'name': '免费观影票', 'category': 2, 'points_price': 3900, 'origin_price_fen': 3900,
        'stock_total': 40, 'stock': 8, 'daily_limit': 1, 'expire_days': 30, 'badge': 'NEW', 'sort': 2,
        'description': '兑换即得 1 张免费观影票，2D 场次任选（IMAX/巨幕等特殊制式补差价）。',
        'notes': '兑换后不可撤销；券码到店/线上核销；30 天有效；每人每日限兑 1 张，库存有限兑完即止。',
        'applicable': '支持 2D 普通场次，特殊制式需补差价。',
    },
]


def forwards(apps, schema_editor):
    MallItem = apps.get_model('points', 'MallItem')
    for row in SEED:
        if MallItem.objects.filter(name=row['name'], deleted=0).exists():
            continue
        MallItem.objects.create(**row, status=1, deleted=0)


def backwards(apps, schema_editor):
    pass  # 数据种子不回滚，避免误删运营后续编辑过的同名商品


class Migration(migrations.Migration):

    dependencies = [
        ('points', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
