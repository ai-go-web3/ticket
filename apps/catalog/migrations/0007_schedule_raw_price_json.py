"""新增排片原始价快照字段 raw_price_json（麻花未上浮成本口径，对账/审计用）。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('catalog', '0006_movie_no_show_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='schedule',
            name='raw_price_json',
            field=models.JSONField(blank=True, null=True, verbose_name='麻花原始价快照'),
        ),
    ]
