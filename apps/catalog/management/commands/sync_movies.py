"""同步影片：从麻花真实接口拉取热映/待映电影并入库。

流程：登录取 token → 热映(movieOnInfoList, ci=城市id) → 待映(comingList, pageNum)
     → 按 up_movie_id upsert 到 movie 表。

presale 规则（驱动前端 buy_tag：提前购/特惠购/预约）：
  上映日期晚于今天 = 尚未上映 = 预售(presale=1)，否则 presale=0。

用法：
  python manage.py sync_movies --settings=config.settings_dev            # 默认成都(cityId=8)
  python manage.py sync_movies --settings=config.settings_dev --city 8 --pages 1
"""
from datetime import date, datetime

from django.core.management.base import BaseCommand

from apps.catalog.models import Movie
from apps.upadapter.mahua import MahuaClient
from apps.upadapter.token import get_token


def _parse_release(value):
    """'2026-10-01 00:00:00' -> date，失败 None。"""
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], '%Y-%m-%d').date()
    except (ValueError, TypeError):
        return None


class Command(BaseCommand):
    help = '从麻花真实接口同步热映/待映电影入库（默认成都 cityId=8）'

    def add_arguments(self, parser):
        parser.add_argument('--city', default='8', help='麻花城市ID（成都=8）')
        parser.add_argument('--pages', type=int, default=1, help='待映拉取页数')

    def handle(self, *args, **options):
        city_id = options['city']
        pages = options['pages']
        client = MahuaClient()

        token = get_token()
        if not token:
            self.stderr.write(self.style.ERROR('麻花 token 获取失败，无法同步'))
            return
        self.stdout.write(f'使用麻花 token={token[:8]}...')

        today = date.today()
        hot_created, hot_updated = self._sync_list(
            client.get_hot_movies(token, city_id), Movie.STATUS_HOT, today)
        self.stdout.write(self.style.SUCCESS(
            f'热映同步完成：新增 {hot_created}，更新 {hot_updated}'))

        com_created = com_updated = 0
        for p in range(1, pages + 1):
            c, u = self._sync_list(
                client.get_coming_movies(token, p), Movie.STATUS_COMING, today)
            com_created += c
            com_updated += u
        self.stdout.write(self.style.SUCCESS(
            f'待映同步完成：新增 {com_created}，更新 {com_updated}'))

    def _sync_list(self, result, status, today):
        """处理 (rtnCode, rtnData) 并 upsert，返回 (created, updated)。"""
        code, data = result
        if code != '000000' or not data:
            self.stderr.write(self.style.WARNING(f'接口返回异常: code={code}'))
            return 0, 0
        if isinstance(data, str):
            import json
            data = json.loads(data)

        created = updated = 0
        for item in data:
            mid = str(item.get('id', ''))
            if not mid:
                continue
            grade = item.get('grade')
            release = _parse_release(item.get('publishTime'))
            wish = item.get('wishNum')
            defaults = {
                'name': item.get('name', ''),
                'status': status,
                'type': item.get('filmTypes'),
                'duration': item.get('duration'),
                'rating': grade if grade not in (None, '') else None,
                'director': item.get('director') or None,
                'actors': item.get('cast') or None,
                'language': item.get('language') or None,
                'poster_url': item.get('pic') or None,
                'description': item.get('intro') or None,
                'release_date': release,
                'want_count': int(wish) if wish not in (None, '') else 0,
                'presale': 1 if (release and release > today) else 0,
                'deleted': 0,
            }
            _, is_new = Movie.objects.update_or_create(
                up_movie_id=mid, defaults=defaults)
            if is_new:
                created += 1
            else:
                updated += 1
        return created, updated
