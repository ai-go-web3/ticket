"""Seed 5 档默认会员等级（对齐旧 settings.POINTS['TIERS']）。

新项目未上线，无历史数据；线上部署时随 makemigrations→migrate 自动落库。
如运营后续在后台改，本迁移不回滚（reverse 只清空，不重置为 settings 值）。
"""
from decimal import Decimal

from django.db import migrations


LEVELS = [
    # level, name, growth_min, consume_rate, color, perks
    (0, '新影迷',   0,     Decimal('0'),     '#8a94a6', '基础消费返积分 · 积分可兑商城权益'),
    (1, '常客',     500,   Decimal('0'),     '#ff6ea3', '消费返积分 · 生日券 · 优先客服'),
    (2, '资深影迷', 2000,  Decimal('0'),     '#ff2f6d', '常客权益 + 每月小食券 · 特效厅折扣'),
    (3, '超级影迷', 6000,  Decimal('0'),     '#ffb020', '资深权益 + 优先场券 · 双人套票折扣'),
    (4, '骨灰影迷', 15000, Decimal('0'),     '#a855f7', '超级权益 + 限量周边 · 首映礼邀请'),
]


def seed(apps, schema_editor):
    MemberLevel = apps.get_model('distributor', 'MemberLevel')
    for level, name, growth_min, rate, color, perks in LEVELS:
        MemberLevel.objects.update_or_create(
            level=level,
            defaults={
                'name': name, 'growth_min': growth_min, 'consume_rate': rate,
                'color': color, 'perks': perks, 'is_active': 1, 'sort': level,
            },
        )


def unseed(apps, schema_editor):
    MemberLevel = apps.get_model('distributor', 'MemberLevel')
    MemberLevel.objects.filter(level__in=[lv for lv, *_ in LEVELS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('distributor', '0005_memberlevel'),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
