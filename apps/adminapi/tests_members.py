"""会员管理接口冲烟测试（临时；跑完删除）。

用 settings_dev（SQLite）跑，测试库由 migrations 现场建，避免线上 MySQL 陈旧 schema 干扰。
命令：venv/bin/python manage.py test apps.adminapi.tests_members --settings=config.settings_dev
"""
import json
from django.utils import timezone
from rest_framework.test import APITestCase, APIRequestFactory, force_authenticate

from apps.adminapi.models import AdminUser
from apps.adminapi import views_members as V
from apps.auths.models import AppUser
from apps.distributor.models import Wallet, WalletTxn, MemberLevel
from apps.order.models import TicketOrder

factory = APIRequestFactory()


class MembersSmokeTest(APITestCase):
    def setUp(self):
        self.admin = AdminUser.objects.create(username='smoke', password='x', is_active=1)
        self.u1 = AppUser.objects.create(openid='o1', phone='13800001111', phone_mask='138****1111',
                                         nickname='小明', city_code='510100', status=1,
                                         last_login_at=timezone.now(), deleted=0)
        self.u2 = AppUser.objects.create(openid='o2', phone='13800002222', phone_mask='138****2222',
                                         nickname='小红', status=1, last_login_at=None, deleted=0)
        AppUser.objects.create(openid='o3', phone='13800003333', phone_mask='138****3333',
                               nickname='冻结者', status=2, deleted=0)
        self.u4 = AppUser.objects.create(openid='o4', phone='13800004444', phone_mask='138****4444',
                                         nickname='注销者', status=1, deleted=1)

        MemberLevel.objects.filter(level=1).delete()
        MemberLevel.objects.create(level=1, name='黄金会员', growth_min=0, color='#ffb020', is_active=1)

        self.w1 = Wallet.objects.create(user_id=self.u1.id, balance=350, growth=1200, tier=1)
        Wallet.objects.create(user_id=self.u2.id, balance=0, growth=0, tier=0)

        TicketOrder.objects.create(order_ext_no='A1', user_id=self.u1.id, schedule_id=1, cinema_id=0,
                                   movie_id=0, up_schedule_id='x', seats_json=[], seat_count=1,
                                   ticket_amount=5000, pay_amount=4500,
                                   status=TicketOrder.STATUS_DONE, mobile='13800001111')
        TicketOrder.objects.create(order_ext_no='A2', user_id=self.u1.id, schedule_id=1, cinema_id=0,
                                   movie_id=0, up_schedule_id='x', seats_json=[], seat_count=2,
                                   ticket_amount=8000, pay_amount=7600,
                                   status=TicketOrder.STATUS_SCREENED, mobile='13800001111')
        TicketOrder.objects.create(order_ext_no='A3', user_id=self.u1.id, schedule_id=1, cinema_id=0,
                                   movie_id=0, up_schedule_id='x', seats_json=[], seat_count=1,
                                   ticket_amount=3000, pay_amount=0,
                                   status=TicketOrder.STATUS_PAYING, mobile='13800001111')

        WalletTxn.objects.create(wallet_id=self.w1.id, biz_type=WalletTxn.BIZ_SETTLE,
                                 scene=WalletTxn.SCENE_CONSUME, amount=35, balance_after=350)
        WalletTxn.objects.create(wallet_id=self.w1.id, biz_type=WalletTxn.BIZ_FREEZE,
                                 scene=0, amount=-100, balance_after=250)

    def _req(self, method, path, query=None, body=None):
        req = factory.get(path, query or {}) if method == 'get' else \
            factory.post(path, data=json.dumps(body or {}), content_type='application/json')
        force_authenticate(req)
        req.admin = self.admin
        return req

    def test_all_endpoints(self):
        r = V.members(self._req('get', '/m'))
        assert r.status_code == 200, r.status_code
        d = r.data['data']
        ids = {it['id'] for it in d['items']}
        assert self.u1.id in ids and self.u4.id in ids
        row1 = next(it for it in d['items'] if it['id'] == self.u1.id)
        assert row1['order_count'] == 3, row1
        assert row1['total_spent_fen'] == 12100, row1
        assert row1['tier_name'] == '黄金会员', row1
        assert row1['status_text'] == '正常', row1

        r = V.members_status_count(self._req('get', '/mc'))
        cc = {i['status']: i['count'] for i in r.data['data']['items']}
        assert cc[0] >= 1 and cc[2] >= 1, cc

        r = V.members(self._req('get', '/m', query={'kw': '小明', 'status': '1'}))
        assert r.data['data']['total'] == 1, r.data['data']

        r = V.members(self._req('get', '/m', query={'tier': '1'}))
        assert self.u1.id in {it['id'] for it in r.data['data']['items']}

        r = V.member_detail(self._req('get', f'/md/{self.u1.id}'), member_id=self.u1.id)
        dd = r.data['data']
        assert dd['phone'] == '13800001111'
        assert len(dd['recent_orders']) == 3
        assert all('status_text' in o for o in dd['recent_orders'])
        assert len(dd['points_txns']) == 1, dd['points_txns']  # 排除 BIZ_FREEZE
        assert dd['points_txns'][0]['desc'] == '消费返积分', dd['points_txns'][0]

        r = V.member_detail(self._req('get', f'/md/{self.u2.id}'), member_id=self.u2.id)
        dd2 = r.data['data']
        assert dd2['last_login_at'] is None and dd2['total_spent_fen'] == 0

        r = V.member_freeze(self._req('post', f'/f/{self.u1.id}'), member_id=self.u1.id)
        assert r.data['data']['status'] == 2
        assert AppUser.objects.get(id=self.u1.id).status == 2

        r = V.member_unfreeze(self._req('post', f'/u/{self.u1.id}'), member_id=self.u1.id)
        assert r.data['data']['status'] == 1

        r = V.member_freeze(self._req('post', f'/f/{self.u4.id}'), member_id=self.u4.id)
        assert r.data['data'] is None
        print('\n==== ALL SMOKE CHECKS PASSED ====')
