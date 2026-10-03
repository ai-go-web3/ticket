"""财务快照字段：建单时上浮费率 price_rate 与预估成本 est_cost_amount（对账/盈利报表用）。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('order', '0002_remove_ticketorder_lock_token'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketorder',
            name='price_rate',
            field=models.FloatField(null=True, verbose_name='成本上浮费率快照'),
        ),
        migrations.AddField(
            model_name='ticketorder',
            name='est_cost_amount',
            field=models.BigIntegerField(null=True, verbose_name='预估成本(分,Σ原始fastPrice)'),
        ),
    ]
