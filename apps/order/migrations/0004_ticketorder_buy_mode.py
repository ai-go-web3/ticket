"""新增购票模式 buy_mode：特惠（放单默认 0-特惠模式）/ 快速（放单 model=2 极速模式）。"""
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('order', '0003_price_rate_est_cost'),
    ]

    operations = [
        migrations.AddField(
            model_name='ticketorder',
            name='buy_mode',
            field=models.CharField(default='tehui', max_length=8, verbose_name='购票模式'),
        ),
    ]
