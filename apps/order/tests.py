"""order 放单收敛测试：第三方调用失败的即时重试/退款策略。

策略（需求口径）：放单调用第三方失败，先查询确认受理状态（超时单可能已到达），
查无此单立即重放一次；重放仍失败 -> 出票失败(60) + 自动退款。全生命周期至多重放
一次（MahuaDispatch.retry_count 兜底），不做多轮补偿重试。
"""
import json
from unittest import mock

from django.conf import settings as dj_settings
from django.test import TestCase, override_settings

from apps.order.models import MahuaDispatch, TicketOrder
from apps.refund.models import Refund
from apps.upadapter import client as dispatch_client
from apps.upadapter.mahua import SUCCESS_CODE


def _mahua_settings():
    m = dict(dj_settings.MAHUA)
    m.update({'BASE_URL': 'http://mahua.test', 'DISPATCH_ENABLED': True,
              'CALLBACK_URL': '', 'COST_TOTAL_PRICE': ''})
    return m


def _make_order(ext_no='PP20261004000001'):
    return TicketOrder.objects.create(
        order_ext_no=ext_no, user_id=1, schedule_id=1, cinema_id=1,
        movie_id=1, up_schedule_id='UP1',
        seats_json=json.dumps([{'row': 4, 'col': 1, 'name': '4排2座'}]),
        seat_count=1, ticket_amount=4500, pay_amount=4500,
        mobile='13800000000', status=TicketOrder.STATUS_DISPATCHING,
    )


@override_settings(MAHUA=_mahua_settings())
class DispatchConvergeTests(TestCase):
    """dispatch() 对第三方调用失败的即时收敛。"""

    def setUp(self):
        self.order = _make_order()

    def _run(self, dispatch_side, query_side, query_side2=None):
        """mock token/麻花客户端/微信退款后执行 dispatch。

        各 side： [(code, data), ...] 返回值序列，或单个异常实例。
        time.sleep 被 mock（记录调用参数，不真等），可通过 self.sleep_mock 断言。
        微信退款 mock 为受理成功（返回微信退款单号）：订单停在「退款中(70)」，
        到账由 on_refund_notify 迁移——与真实链路终态一致且不依赖证书配置。
        """
        def side(v):
            return v if isinstance(v, list) else [v]

        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC, \
                mock.patch.object(dispatch_client.time, 'sleep') as sleep_mock, \
                mock.patch('apps.pay.services.wx_refund',
                           return_value=(True, 'WXREFUND1')):
            self.sleep_mock = sleep_mock
            inst = MC.return_value
            inst.dispatch.side_effect = side(dispatch_side)
            inst.query_order.side_effect = side(query_side) + (side(query_side2) if query_side2 else [])
            return dispatch_client.dispatch(self.order.order_ext_no)

    def _assert_refunded(self, retry_count):
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 1)
        d = MahuaDispatch.objects.get(order_ext_no=self.order.order_ext_no)
        self.assertEqual(d.dispatch_status, MahuaDispatch.STATUS_FAIL)
        self.assertEqual(d.retry_count, retry_count)

    def test_timeout_but_accepted_converges_by_query(self):
        """首次请求超时：等 20s 再查，发现麻花已受理则按查询收敛，不重放不退款。"""
        with mock.patch.object(dispatch_client, 'query_and_sync') as qs:
            ok, _no = self._run(
                Exception('timeout'),
                [(SUCCESS_CODE, {'status': 'drawSuccess', 'tickets': []})],
            )
        self.assertTrue(ok)
        # 超时后先等 20s 再查单（给麻花受理时间）
        self.sleep_mock.assert_called_once_with(20)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_DISPATCHING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 0)
        d = MahuaDispatch.objects.get(order_ext_no=self.order.order_ext_no)
        self.assertEqual(d.dispatch_status, MahuaDispatch.STATUS_DISPATCHED)
        self.assertEqual(d.retry_count, 0)
        qs.assert_called_once_with(self.order.order_ext_no)

    def test_not_accepted_redispatch_once_success(self):
        """查无此单：等 20s 查询后立即重放一次且成功；retry_count 记 1。"""
        ok, no = self._run(
            [Exception('timeout'), (SUCCESS_CODE, 'M123')],
            [(9999, {'msg': 'not found'})],
        )
        self.assertTrue(ok)
        self.assertEqual(no, 'M123')
        # 查单前等待 20s
        self.sleep_mock.assert_called_once_with(20)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_DISPATCHING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 0)
        d = MahuaDispatch.objects.get(order_ext_no=self.order.order_ext_no)
        self.assertEqual(d.dispatch_status, MahuaDispatch.STATUS_DISPATCHED)
        self.assertEqual(d.retry_count, 1)

    def test_redispatch_fail_then_refund(self):
        """重放仍失败：不再重试，订单转出票失败并自动退款。"""
        ok, _no = self._run(
            [Exception('timeout'), Exception('timeout again')],
            [(9999, {'msg': 'not found'})],
            [(9999, {'msg': 'not found'})],
        )
        self.assertFalse(ok)
        # 两次超时各自等 20s 后查单
        self.assertEqual(self.sleep_mock.call_count, 2)
        self._assert_refunded(retry_count=1)

    def test_clear_reject_refunds_immediately(self):
        """明确被拒（rtnCode 非成功）：麻花未受理，直接出票失败并退款，不重放不等待。"""
        ok, _no = self._run([(100008, {'msg': 'seatId invalid'})], [(9999, {'msg': 'not found'})])
        self.assertFalse(ok)
        self.sleep_mock.assert_not_called()
        self._assert_refunded(retry_count=0)


@override_settings(MAHUA=_mahua_settings())
class CompensationBoundedRetryTests(TestCase):
    """补偿任务重放至多一次：超限转出票失败退款。"""

    def test_pending_over_retry_limit_refunds(self):
        self.order = _make_order('PP20261004000002')
        MahuaDispatch.objects.create(
            order_ext_no=self.order.order_ext_no, request_body={},
            dispatch_status=MahuaDispatch.STATUS_PENDING, retry_count=1,
        )
        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC, \
                mock.patch('apps.pay.services.wx_refund',
                           return_value=(True, 'WXREFUND1')):
            inst = MC.return_value
            inst.query_order.return_value = (9999, {'msg': 'not found'})
            res = dispatch_client.compensate_dispatches()
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 1)
        self.assertEqual(res['failed'], 1)
        self.assertEqual(res['redispatched'], 0)

    def test_pending_within_limit_redispatches_once(self):
        self.order = _make_order('PP20261004000003')
        MahuaDispatch.objects.create(
            order_ext_no=self.order.order_ext_no, request_body={},
            dispatch_status=MahuaDispatch.STATUS_PENDING, retry_count=0,
        )
        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC:
            inst = MC.return_value
            inst.query_order.return_value = (9999, {'msg': 'not found'})
            inst.dispatch.return_value = (SUCCESS_CODE, 'M456')
            res = dispatch_client.compensate_dispatches()
        self.assertEqual(res['redispatched'], 1)
        d = MahuaDispatch.objects.get(order_ext_no=self.order.order_ext_no)
        self.assertEqual(d.dispatch_status, MahuaDispatch.STATUS_DISPATCHED)
        self.assertEqual(d.retry_count, 1)


@override_settings(MAHUA=_mahua_settings())
class DispatchEventConvergeTests(TestCase):
    """查询/回调共用收敛方法的幂等：先处理者生效，后到者不重复动作。"""

    def setUp(self):
        self.order = _make_order('PP20261004000011')
        # 放单映射随真实放单落库，查询/回调收敛会更新它
        MahuaDispatch.objects.create(
            order_ext_no=self.order.order_ext_no, request_body={},
            dispatch_status=MahuaDispatch.STATUS_DISPATCHED,
        )

    def test_drawclose_once_query_then_callback(self):
        """drawClose 查询先处理退款后，回调再到不得二次退款。"""
        with mock.patch('apps.pay.services.wx_refund',
                        return_value=(True, 'WXREFUND1')):
            # 第一路（查询轮询）先收敛 drawClose
            dispatch_client._handle_dispatch_event(
                self.order, dispatch_client.CALLBACK_DRAW_CLOSE, {})
            # 第二路（回调）后到
            dispatch_client.on_order_callback(
                {'outId': self.order.order_ext_no,
                 'status': dispatch_client.CALLBACK_DRAW_CLOSE, 'note': '出票失败'})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 1)
        d = MahuaDispatch.objects.get(order_ext_no=self.order.order_ext_no)
        self.assertEqual(d.dispatch_status, MahuaDispatch.STATUS_FAIL)

    def test_drawclose_once_callback_then_query(self):
        """drawClose 回调先处理退款后，查询再到不得二次退款。"""
        with mock.patch('apps.pay.services.wx_refund',
                        return_value=(True, 'WXREFUND1')):
            dispatch_client.on_order_callback(
                {'outId': self.order.order_ext_no,
                 'status': dispatch_client.CALLBACK_DRAW_CLOSE, 'note': '出票失败'})
            dispatch_client._handle_dispatch_event(
                self.order, dispatch_client.CALLBACK_DRAW_CLOSE, {})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 1)

    def test_drawsuccess_idempotent_across_paths(self):
        """drawSuccess 查询先回填票券后，回调再到不重复建票/二次迁移。"""
        tickets = [{'ticketInfo': '88236641', 'ticketImg': 'bar'}]
        data = {'status': 'drawSuccess', 'confirmPrice': 45,
                'tickets': tickets, 'realSeats': '4排2座'}
        dispatch_client._handle_dispatch_event(self.order, 'drawSuccess', data)
        dispatch_client.on_order_callback(
            {'outId': self.order.order_ext_no, 'status': 'updateTicket',
             'confirmPrice': 45, 'tickets': tickets, 'realSeats': '4排2座'})
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_WAIT_PICK)
        self.assertEqual(self.order.settle_amount, 4500)
        from apps.order.models import Ticket
        self.assertEqual(Ticket.objects.filter(order_id=self.order.id).count(), 1)
        self.assertEqual(Ticket.objects.get(order_id=self.order.id).ticket_code, '88236641')


@override_settings(MAHUA=_mahua_settings())
class DispatchPollingTests(TestCase):
    """每 2 分钟 /put/query 轮询任务。"""

    def test_polling_converges_ticketed_order(self):
        """轮询查到 drawSuccess：订单转待取票并回填票券。"""
        self.order = _make_order('PP20261004000021')
        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC:
            inst = MC.return_value
            inst.query_order.return_value = (SUCCESS_CODE, {
                'status': 'drawSuccess', 'confirmPrice': 45,
                'tickets': [{'ticketInfo': '88236642', 'ticketImg': 'bar'}],
                'realSeats': '4排2座',
            })
            res = dispatch_client.query_dispatching_orders()
        self.assertEqual(res['queried'], 1)
        self.assertEqual(res['synced'], 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_WAIT_PICK)

    def test_polling_skips_not_found(self):
        """轮询查无此单（放单未受理）：跳过，不动订单状态。"""
        self.order = _make_order('PP20261004000022')
        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC:
            inst = MC.return_value
            inst.query_order.return_value = (9999, {'msg': 'not found'})
            res = dispatch_client.query_dispatching_orders()
        self.assertEqual(res['synced'], 0)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_DISPATCHING)

    def test_polling_refunds_drawclose_once(self):
        """轮询查到 drawClose：退款一次；同单回调再来不再退。"""
        self.order = _make_order('PP20261004000023')
        with mock.patch.object(dispatch_client, 'get_token', return_value='tok'), \
                mock.patch.object(dispatch_client, 'MahuaClient') as MC, \
                mock.patch('apps.pay.services.wx_refund',
                           return_value=(True, 'WXREFUND1')):
            inst = MC.return_value
            inst.query_order.return_value = (SUCCESS_CODE, {'status': 'drawClose'})
            res = dispatch_client.query_dispatching_orders()
            # 回调随后到达
            dispatch_client.on_order_callback(
                {'outId': self.order.order_ext_no,
                 'status': dispatch_client.CALLBACK_DRAW_CLOSE, 'note': '出票失败'})
        self.assertEqual(res['synced'], 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(Refund.objects.filter(order_id=self.order.id).count(), 1)
