"""同步城市：用 city_mapping 静态映射表灌入 city 表（以麻花 cityId 为准）。"""
from django.core.management.base import BaseCommand

from apps.catalog.models import City
from apps.catalog.city_mapping import MAHUA_CITY_MAP, HOT_CITY_IDS, mahua_to_std


class Command(BaseCommand):
    help = '从 city_mapping 静态表同步城市到 city 表（麻花 cityId 为权威主键）'

    def handle(self, *args, **options):
        created = updated = 0
        for city_id, (name, std_code) in MAHUA_CITY_MAP.items():
            defaults = {
                'city_name': name,
                'std_code': std_code or None,
                'hot': 1 if city_id in HOT_CITY_IDS else 0,
            }
            obj, is_new = City.objects.update_or_create(
                city_code=city_id, defaults=defaults,
            )
            if is_new:
                created += 1
            else:
                updated += 1
        self.stdout.write(self.style.SUCCESS(
            f'城市同步完成：新增 {created}，更新 {updated}，共 {len(MAHUA_CITY_MAP)} 个城市'
        ))
