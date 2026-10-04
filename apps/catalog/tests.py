"""catalog 视图测试：cinemas 麻花权威路径 + 失败回退路径。"""
from unittest import mock

from django.test import TestCase

from apps.catalog.models import Cinema, Movie
from apps.catalog import services as catalog_services


def _mahua_row(cinema_id, name, km, refund=True):
    return {
        'cinemaId': cinema_id,
        'cinemaName': name,
        'cinemaAddress': '测试路1号',
        'standardId': '510100',
        'distancekm': str(km),
        'lng': '104.08',
        'lat': '30.65',
        'regionName': '锦江区',
        'brand': '万达',
        'refundStatus': refund,
    }


class CinemasViewTests(TestCase):
    def setUp(self):
        # 注意顺序：本地行按 up_cinema_id 查，与麻花返回顺序无关
        self.c_b = Cinema.objects.create(
            up_cinema_id='202', name='B影城', city_code='8',
            brand='万达', region='锦江区', lng=104.08, lat=30.65,
        )
        self.c_a = Cinema.objects.create(
            up_cinema_id='101', name='A影城', city_code='8',
            brand='SFC', region='青羊区', lng=104.09, lat=30.66,
        )

    @mock.patch.object(catalog_services, 'pull_cinemas')
    def test_mahua_authoritative_order_and_fields(self, mock_pull):
        """麻花成功：过滤/排序以麻花返回顺序为准，本地仅映射 id/补字段。"""
        mock_pull.return_value = [
            _mahua_row(202, 'B影城', 1.2),
            _mahua_row(101, 'A影城', 0.5),
        ]
        resp = self.client.get('/api/v1/catalog/cinemas', {
            'cityCode': '8', 'movieId': '4', 'date': '2026-10-03',
            'lng': '104.081', 'lat': '30.6526', 'orderBy': 'distance',
        })
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['data'])
        items = resp.json()['data']
        # 顺序 = 麻花返回顺序（B 在前），而非本地主键序
        self.assertEqual([i['name'] for i in items], ['B影城', 'A影城'])
        # 主键映射到本地 id
        self.assertEqual([i['id'] for i in items], [self.c_b.id, self.c_a.id])
        # distancekm 换算为米
        self.assertEqual(items[0]['distance'], 1200)
        # refundStatus 透出
        self.assertTrue(items[0]['refundable'])
        # 麻花收到完整下推参数
        kwargs = mock_pull.call_args.kwargs
        self.assertEqual(kwargs['sort_by'], 'distance')
        self.assertEqual(kwargs['lng'], 104.081)
        self.assertEqual(kwargs['lat'], 30.6526)

    @mock.patch.object(catalog_services, 'pull_cinemas')
    def test_brand_and_hall_keyword_passthrough(self, mock_pull):
        """brand/hallKeyword 透传给麻花；brand 空串视为未传。"""
        mock_pull.return_value = []
        self.client.get('/api/v1/catalog/cinemas', {
            'cityCode': '8', 'brand': '万达', 'hallKeyword': 'IMAX,杜比',
        })
        kwargs = mock_pull.call_args.kwargs
        self.assertEqual(kwargs['brand'], '万达')
        self.assertEqual(kwargs['hall_keyword'], ['IMAX', '杜比'])

        self.client.get('/api/v1/catalog/cinemas', {'cityCode': '8', 'brand': ''})
        self.assertIsNone(mock_pull.call_args.kwargs['brand'])

    @mock.patch.object(catalog_services, 'pull_cinemas')
    def test_mahua_unknown_cinema_dropped(self, mock_pull):
        """麻花返回了本地没有的影院（upsert 后仍查不到的脏数据）被丢弃。"""
        mock_pull.return_value = [
            _mahua_row(202, 'B影城', 1.0),
            _mahua_row(999, '幽灵影院', 0.1),
        ]
        resp = self.client.get('/api/v1/catalog/cinemas', {'cityCode': '8'})
        items = resp.json()['data']
        self.assertEqual([i['name'] for i in items], ['B影城'])

    @mock.patch.object(catalog_services, 'pull_cinemas')
    def test_fallback_to_db_on_failure(self, mock_pull):
        """麻花异常：回退本地库过滤（旧路径不回归）。"""
        mock_pull.side_effect = RuntimeError('mahua down')
        resp = self.client.get('/api/v1/catalog/cinemas', {
            'cityCode': '8', 'kw': 'A影城', 'lng': '104.081', 'lat': '30.6526',
        })
        items = resp.json()['data']
        self.assertEqual([i['name'] for i in items], ['A影城'])
        # 回退路径距离为本地 haversine 计算
        self.assertIsNotNone(items[0]['distance'])
        self.assertIsNone(items[0]['refundable'])


class CinemaBrandsViewTests(TestCase):
    def setUp(self):
        Movie.objects.create(id=4, up_movie_id='m4', name='测试片', status=1)
        Cinema.objects.create(up_cinema_id='202', name='B影城', city_code='8', brand='万达')
        Cinema.objects.create(up_cinema_id='101', name='A影城', city_code='8', brand='万达')
        Cinema.objects.create(up_cinema_id='103', name='C影城', city_code='8', brand='CGV')

    @mock.patch.object(catalog_services, 'pull_brands')
    def test_brands_from_mahua(self, mock_pull):
        """麻花成功：直接返回品牌字典（含数量）。"""
        mock_pull.return_value = [{'name': '万达', 'count': 2}, {'name': 'CGV', 'count': 1}]
        resp = self.client.get('/api/v1/catalog/cinema-brands', {'cityCode': '8'})
        self.assertEqual(resp.json()['data'], [{'name': '万达', 'count': 2}, {'name': 'CGV', 'count': 1}])

    @mock.patch.object(catalog_services, 'pull_brands')
    def test_brands_empty_authoritative_with_movie(self, mock_pull):
        """按影片筛选时麻花返回空 = 权威空集，直接返回空列表。"""
        mock_pull.return_value = []
        resp = self.client.get('/api/v1/catalog/cinema-brands', {'cityCode': '8', 'movieId': '4'})
        self.assertEqual(resp.json()['data'], [])

    @mock.patch.object(catalog_services, 'pull_brands')
    def test_brands_fallback_to_db_on_failure(self, mock_pull):
        """未按影片筛选且麻花异常：回退本地 Cinema.brand 统计。"""
        mock_pull.side_effect = RuntimeError('mahua down')
        resp = self.client.get('/api/v1/catalog/cinema-brands', {'cityCode': '8'})
        self.assertEqual(resp.json()['data'], [{'name': '万达', 'count': 2}, {'name': 'CGV', 'count': 1}])
