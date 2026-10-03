"""手动同步影片兜底：从麻花真实接口拉取热映/待映电影并入库。

正常路径是按需实时拉取（catalog.services.pull_movies，列表接口内部自动做），
本命令保留用于运维手动灌数据/排查问题。入库逻辑统一走 services.upsert_movies。

用法：
  python manage.py sync_movies --settings=config.settings_dev            # 默认成都(cityId=8)
  python manage.py sync_movies --settings=config.settings_dev --city 8 --pages 1
"""
from django.core.management.base import BaseCommand

from apps.catalog import services as catalog_services
from apps.catalog.models import Movie
from apps.upadapter.mahua import MahuaClient
from apps.upadapter.token import get_token


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

        from django.utils import timezone
        hot_started = timezone.now()
        hot_created, hot_updated = self._sync_list(
            client.get_hot_movies(token, city_id), Movie.STATUS_HOT)
        retired = catalog_services._retire_stale_hot(hot_started)
        self.stdout.write(self.style.SUCCESS(
            f'热映同步完成：新增 {hot_created}，更新 {hot_updated}，下线 {retired}'))

        com_created = com_updated = 0
        for p in range(1, pages + 1):
            c, u = self._sync_list(
                client.get_coming_movies(token, p), Movie.STATUS_COMING)
            com_created += c
            com_updated += u
        self.stdout.write(self.style.SUCCESS(
            f'待映同步完成：新增 {com_created}，更新 {com_updated}'))

    def _sync_list(self, result, status):
        """处理 (rtnCode, rtnData) 并 upsert，返回 (created, updated)。"""
        items = catalog_services.unwrap_mahua_list(*result)
        if not items:
            self.stderr.write(self.style.WARNING('接口返回异常或为空'))
            return 0, 0
        return catalog_services.upsert_movies(items, status)
