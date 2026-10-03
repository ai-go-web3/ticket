"""移除订单表 lock_token：本地锁座已下线，座位可得性由麻花放单回调收敛。"""
from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('order', '0001_initial'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='ticketorder',
            name='lock_token',
        ),
    ]
