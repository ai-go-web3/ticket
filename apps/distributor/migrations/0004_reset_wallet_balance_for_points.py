"""数据迁移：观影金(分) → 积分体系，旧余额清零。

背景：balance/frozen 语义由「观影金(分)」改为「积分」，单位口径变化。
经确认现网存量≈0，推荐「清零 + 等额抵扣券公告」（券发放属运营动作，不在本迁移）。
本迁移把所有 wallet 的 balance/frozen 归零；growth/total_income 保留（历史累计口径）。
可逆（reverse）：无历史值可还原，回滚同样置 0（幂等安全）。
"""
from django.db import migrations


def reset_balance(apps, schema_editor):
    Wallet = apps.get_model('distributor', 'Wallet')
    Wallet.objects.all().update(balance=0, frozen=0)


def rollback(apps, schema_editor):
    # 旧值不可恢复（未留存），回滚同样清零，保持幂等
    Wallet = apps.get_model('distributor', 'Wallet')
    Wallet.objects.all().update(balance=0, frozen=0)


class Migration(migrations.Migration):

    dependencies = [
        ('distributor', '0003_deductrule_fen_per_point_wallet_growth_and_more'),
    ]

    operations = [
        migrations.RunPython(reset_balance, rollback),
    ]
