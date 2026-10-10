"""数据迁移：修正 MemberLevel 权益文案（积分抵扣购票已下线）。

0006 的种子文案「基础消费返积分 · 购票可抵现金」含已过时的「抵现金」表述。
对已 seed 过的库（含线上），仅改 0006 不会回溯重跑；本迁移以 UPDATE 幂等地把
level=0 的 perks 纠正为不含抵扣的表述。对全新库（0006 已按新文案种入）则等价无副作用。
"""
from django.db import migrations

# level -> 纠正后的权益文案
FIXED_PERKS = {
    0: '基础消费返积分 · 积分可兑商城权益',
}


def forwards(apps, schema_editor):
    MemberLevel = apps.get_model('distributor', 'MemberLevel')
    for level, perks in FIXED_PERKS.items():
        MemberLevel.objects.filter(level=level).update(perks=perks)


def backwards(apps, schema_editor):
    # 回滚为原（含抵扣）文案，仅便于对称；新项目一般无需回滚
    MemberLevel = apps.get_model('distributor', 'MemberLevel')
    MemberLevel.objects.filter(level=0).update(
        perks='基础消费返积分 · 购票可抵现金')


class Migration(migrations.Migration):

    dependencies = [
        ('distributor', '0006_seed_member_levels'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
