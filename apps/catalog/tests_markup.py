"""上浮规则引擎回归测试（P3）。

核心不变式：无任何规则命中时，定价行为与改造前的全局 settings.PRICE_MARKUP_RATE
完全一致（byte-identical）。另覆盖 rate/flat 加价、维度命中、优先级、兜底行、
反推成本（est_cost）以及后台规则 CRUD + 预览接口的连通性。
"""
from unittest import mock

from django.conf import settings
from django.test import TestCase

from apps.catalog import markup
from apps.catalog import services as catalog_services
from apps.catalog.models import MarkupRule, Cinema, Movie, Schedule


def _old_global_uplift(fen):
    """改造前的口径：直接按全局比例上浮。"""
    rate = float(getattr(settings, 'PRICE_MARKUP_RATE', 0.05) or 0)
    return int(round(fen * (1 + rate)))


class FallbackInvariantTests(TestCase):
    """规则表为空时，一切须回退到旧全局行为。"""

    def setUp(self):
        markup.clear_rules_cache()

    def test_markup_fen_rule_none_equals_old_global(self):
        for fen in (1, 100, 3000, 4567, 10500):
            self.assertEqual(catalog_services._markup_fen(fen), _old_global_uplift(fen))

    def test_resolve_empty_table_returns_global_fallback(self):
        mode, rate, flat = markup.resolve_markup()
        self.assertEqual((mode, rate, flat), markup._fallback())
        self.assertEqual(mode, markup.MODE_RATE)
        self.assertEqual(rate, float(settings.PRICE_MARKUP_RATE))

    def test_uplift_via_resolver_matches_old_global(self):
        """走完整解析链（空表）后上浮结果 == 旧全局上浮。"""
        mode, rate, flat = markup.resolve_markup()
        for fen in (2000, 3888, 6000):
            self.assertEqual(markup.uplift_fen(fen, mode, rate, flat), _old_global_uplift(fen))

    def test_reverse_cost_rate_roundtrip_within_1_fen(self):
        mode, rate, flat = markup.resolve_markup()  # 空表 -> rate 兜底
        for cost in (1000, 2888, 4567, 99999):
            sale = markup.uplift_fen(cost, mode, rate, flat)
            back = markup.reverse_cost_fen(sale, mode, rate, flat)
            self.assertLessEqual(abs(back - cost), 1)


class RuleMatchTests(TestCase):
    def setUp(self):
        MarkupRule.objects.all().delete()
        markup.clear_rules_cache()

    def test_flat_mode_uplift_and_exact_reverse(self):
        MarkupRule.objects.create(name='固定+5元', priority=1, is_active=1,
                                  mode=markup.MODE_FLAT, flat_fen=500)
        markup.clear_rules_cache()
        mode, rate, flat = markup.resolve_markup()
        self.assertEqual((mode, flat), (markup.MODE_FLAT, 500))
        self.assertEqual(markup.uplift_fen(10000, mode, rate, flat), 10500)
        # flat 反推精确：sale - flat
        self.assertEqual(markup.reverse_cost_fen(10500, mode, rate, flat), 10000)

    def test_brand_dimension_hit_and_miss(self):
        MarkupRule.objects.create(name='万达+10%', priority=1, is_active=1,
                                  mode=markup.MODE_RATE, rate=0.12, brands=['万达'])
        markup.clear_rules_cache()
        self.assertEqual(markup.resolve_markup(brand='万达')[1], 0.12)
        # 其它品牌不命中 -> 落回全局兜底 0.05
        self.assertEqual(markup.resolve_markup(brand='CGV')[1],
                         float(settings.PRICE_MARKUP_RATE))

    def test_cinema_dimension_hit_and_miss(self):
        MarkupRule.objects.create(name='指定影院+15%', priority=1, is_active=1,
                                  mode=markup.MODE_RATE, rate=0.15, cinema_ids=[42, 43])
        markup.clear_rules_cache()
        # 内部 Cinema.id 命中（存内部主键，非麻花 up_cinema_id）
        self.assertEqual(markup.resolve_markup(cinema_id=42)[1], 0.15)
        self.assertEqual(markup.resolve_markup(cinema_id=43)[1], 0.15)
        # 其它影院不命中 -> 落回全局兜底
        self.assertEqual(markup.resolve_markup(cinema_id=99)[1],
                         float(settings.PRICE_MARKUP_RATE))
        # 无影院上下文时，含 cinema_ids 的规则不命中（保守）
        self.assertEqual(markup.resolve_markup()[1],
                         float(settings.PRICE_MARKUP_RATE))

    def test_priority_lower_wins(self):
        MarkupRule.objects.create(name='高优先', priority=1, is_active=1,
                                  mode=markup.MODE_RATE, rate=0.30)
        MarkupRule.objects.create(name='低优先', priority=5, is_active=1,
                                  mode=markup.MODE_RATE, rate=0.10)
        markup.clear_rules_cache()
        self.assertEqual(markup.resolve_markup()[1], 0.30)

    def test_inactive_rule_ignored(self):
        MarkupRule.objects.create(name='停用', priority=1, is_active=0,
                                  mode=markup.MODE_RATE, rate=0.30)
        markup.clear_rules_cache()
        self.assertEqual(markup.resolve_markup()[1], float(settings.PRICE_MARKUP_RATE))

    def test_fallback_row_used_when_no_dim_match(self):
        MarkupRule.objects.create(name='兜底', priority=999, is_active=1,
                                  is_fallback=1, mode=markup.MODE_RATE, rate=0.08)
        MarkupRule.objects.create(name='指定片', priority=1, is_active=1,
                                  mode=markup.MODE_RATE, rate=0.20, movie_ids=[777])
        markup.clear_rules_cache()
        self.assertEqual(markup.resolve_markup(movie_id=888)[1], 0.08)  # 落兜底行
        self.assertEqual(markup.resolve_markup(movie_id=777)[1], 0.20)  # 命中特定片


class CreateOrderPricingTests(TestCase):
    """建单链路 est_cost 反推：flat 模式必须 sale-flat，rate/空表须等于旧全局口径。"""

    def setUp(self):
        MarkupRule.objects.all().delete()
        markup.clear_rules_cache()
        self.movie = Movie.objects.create(up_movie_id='m1', name='片', status=1)
        self.cinema = Cinema.objects.create(up_cinema_id='c1', name='场', city_code='8', brand='万达')
        from django.utils import timezone
        from datetime import timedelta
        self.schedule = Schedule.objects.create(
            cinema_id=self.cinema.id, movie_id=self.movie.id, up_schedule_id='s1',
            start_at=timezone.now() + timedelta(hours=3), snapshot_at=timezone.now(),
            sell_status=1, deleted=0,
        )

    def _payload(self, fast):
        return {'scheduleId': self.schedule.id,
                'seats': [{'name': '1排1座', 'row': 1, 'col': 1, 'price': 6000,
                           'salePrice': fast, 'fastPrice': fast, 'maxSpeedPrice': 0}],
                'mobile': '13800000000', 'buyMode': 'tehui'}

    def _order(self):
        from apps.order import services as order_services
        return order_services.create_order(user_id=1, payload=self._payload(10500))

    def test_flat_est_cost_is_sale_minus_flat(self):
        MarkupRule.objects.create(name='固定', priority=1, is_active=1,
                                  mode=markup.MODE_FLAT, flat_fen=500)
        markup.clear_rules_cache()
        order = self._order()
        self.assertEqual(order.est_cost_amount, 10000)   # 10500 - 500
        self.assertEqual(order.price_rate, 0)             # flat 无比例

    def test_empty_table_est_cost_equals_old_global_reverse(self):
        order = self._order()  # 无规则 -> rate 0.05 兜底
        expected = int(round(10500 / (1 + float(settings.PRICE_MARKUP_RATE))))
        self.assertEqual(order.est_cost_amount, expected)
        self.assertEqual(order.price_rate, float(settings.PRICE_MARKUP_RATE))


class AdminRulesApiTests(TestCase):
    """后台规则 CRUD + 预览接口连通性（登录取 admin token）。"""

    def setUp(self):
        MarkupRule.objects.all().delete()
        markup.clear_rules_cache()
        from apps.adminapi.models import AdminUser
        from apps.adminapi.authentication import gen_admin_token
        from django.contrib.auth.hashers import make_password
        self.admin = AdminUser.objects.create(username='qa', password=make_password('pw123456'))
        self.h = {'HTTP_AUTHORIZATION': f'Bearer {gen_admin_token(self.admin.id)}'}

    def test_crud_and_preview(self):
        import json as _json

        def post(path, body):
            return self.client.post(path, data=_json.dumps(body),
                                    content_type='application/json', **self.h)

        def put(path, body):
            return self.client.put(path, data=_json.dumps(body),
                                   content_type='application/json', **self.h)

        # 建
        r = post('/api/v1/admin/rules/create',
                 {'name': '新片+12%', 'priority': 1, 'mode': 'rate', 'rate': '0.12'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['code'], 0)
        rid = r.json()['data']['id']
        # 列表
        r = self.client.get('/api/v1/admin/rules', **self.h)
        self.assertEqual(len(r.json()['data']['items']), 1)
        # 更新
        r = put(f'/api/v1/admin/rules/{rid}', {'rate': '0.15'})
        self.assertEqual(float(r.json()['data']['rate']), 0.15)
        # 预览（无场次上下文，直读兜底应命中刚建的“不限维度”规则=0.15）
        r = post('/api/v1/admin/rules/preview', {'cost_fen': 10000})
        self.assertEqual(r.json()['data']['sale_fen'], 11500)
        # 启停
        r = post(f'/api/v1/admin/rules/{rid}/toggle', {})
        self.assertEqual(r.json()['data']['is_active'], 0)
        # 删
        r = self.client.delete(f'/api/v1/admin/rules/{rid}/delete', **self.h)
        self.assertEqual(r.json()['code'], 0)
        self.assertEqual(MarkupRule.objects.filter(id=rid, deleted=1).count(), 1)

    def test_flat_rule_without_flat_fen_rejected(self):
        import json as _json
        r = self.client.post('/api/v1/admin/rules/create',
                             data=_json.dumps({'name': '坏', 'priority': 1, 'mode': 'flat'}),
                             content_type='application/json', **self.h)
        self.assertNotEqual(r.json()['code'], 0)
