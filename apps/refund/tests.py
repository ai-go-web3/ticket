"""退款对账测试：微信「查询退款」兜底收敛「退款中」订单。

背景：退款到账依赖微信退款结果通知回写；通知丢失时订单停留「退款中(70)」
而钱已到账。reconcile_refunding_refunds 主动调 /pay/refundquery 对账：
SUCCESS/CHANGE -> 已退款(80)；FAIL/REFUNDCLOSE -> 退款单记 FAIL；PROCESSING 跳过。
"""
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from apps.order.models import TicketOrder
from apps.refund.models import Refund, Dispute, DisputeMsg
from apps.refund.services import reconcile_refunding_refunds


def _make_refunding_order(ext_no='PP20261004000031'):
    """一笔「退款中」订单 + 「退款中」退款单（已过 60s 对账宽限期）。"""
    order = TicketOrder.objects.create(
        order_ext_no=ext_no, user_id=1, schedule_id=1, cinema_id=1,
        movie_id=1, up_schedule_id='UP1', seats_json='[]', seat_count=1,
        ticket_amount=1, pay_amount=1, mobile='13800000000',
        status=TicketOrder.STATUS_REFUNDING, pay_status=TicketOrder.PAY_DONE,
    )
    refund = Refund.objects.create(
        refund_ext_no='RF20261004000001', order_id=order.id,
        type=Refund.TYPE_DISPATCH_FAIL, refund_amount=1,
        status=Refund.STATUS_REFUNDING,
    )
    Refund.objects.filter(id=refund.id).update(
        updated_at=timezone.now() - timedelta(seconds=120))
    refund.refresh_from_db()
    return order, refund


class RefundReconcileTests(TestCase):

    def test_arrived_reconciles_order(self):
        """查询到 SUCCESS：退款单记已到账，订单收敛「已退款(80)」+ 支付状态回冲。"""
        order, refund = _make_refunding_order()
        with mock.patch('apps.pay.services.wx_refund_query',
                        return_value='SUCCESS') as q:
            res = reconcile_refunding_refunds()
        q.assert_called_once_with(order, refund)
        self.assertEqual(res['arrived'], 1)
        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, TicketOrder.STATUS_REFUNDED)
        self.assertEqual(order.pay_status, TicketOrder.PAY_REFUNDED)
        self.assertEqual(refund.status, Refund.STATUS_ARRIVED)

    def test_change_counts_as_arrived(self):
        """CHANGE（退款异常退回用户卡）：视为到账。"""
        order, refund = _make_refunding_order('PP20261004000032')
        with mock.patch('apps.pay.services.wx_refund_query', return_value='CHANGE'):
            res = reconcile_refunding_refunds()
        self.assertEqual(res['arrived'], 1)
        order.refresh_from_db()
        self.assertEqual(order.status, TicketOrder.STATUS_REFUNDED)

    def test_processing_stays_refunding(self):
        """PROCESSING：跳过，订单停在「退款中」等下一轮。"""
        order, refund = _make_refunding_order('PP20261004000033')
        with mock.patch('apps.pay.services.wx_refund_query',
                        return_value='PROCESSING'):
            res = reconcile_refunding_refunds()
        self.assertEqual(res['processing'], 1)
        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(refund.status, Refund.STATUS_REFUNDING)

    def test_fail_marks_retryable(self):
        """FAIL/REFUNDCLOSE：退款单记 FAIL 交重试任务，订单停「退款中」。"""
        order, refund = _make_refunding_order('PP20261004000034')
        with mock.patch('apps.pay.services.wx_refund_query',
                        return_value='REFUNDCLOSE'):
            res = reconcile_refunding_refunds()
        self.assertEqual(res['failed'], 1)
        order.refresh_from_db()
        refund.refresh_from_db()
        self.assertEqual(order.status, TicketOrder.STATUS_REFUNDING)
        self.assertEqual(refund.status, Refund.STATUS_FAIL)

    def test_fresh_refund_within_grace_skipped(self):
        """刚受理（60s 宽限期内）的退款单不对账，给通知回写留时间。"""
        order = TicketOrder.objects.create(
            order_ext_no='PP20261004000035', user_id=1, schedule_id=1,
            cinema_id=1, movie_id=1, up_schedule_id='UP1', seats_json='[]',
            seat_count=1, ticket_amount=1, pay_amount=1, mobile='13800000000',
            status=TicketOrder.STATUS_REFUNDING, pay_status=TicketOrder.PAY_DONE,
        )
        Refund.objects.create(
            refund_ext_no='RF20261004000002', order_id=order.id,
            type=Refund.TYPE_DISPATCH_FAIL, refund_amount=1,
            status=Refund.STATUS_REFUNDING,
        )
        with mock.patch('apps.pay.services.wx_refund_query') as q:
            res = reconcile_refunding_refunds()
        q.assert_not_called()
        self.assertEqual(res['refunding'], 0)


# ---- 待取票纠纷退票（dispute_config / apply_dispute / can_dispute）----

from apps.common.response import BizError
from apps.order.statemachine import can_dispute
from apps.refund.services import dispute_config


def _make_wait_pick_order(ext_no='PP20261007000001', minutes_before_show=180):
    """一笔「待取票」订单 + 放单映射（纠纷发起依赖放单号），开场默认 3 小时后。"""
    from apps.order.models import TicketOrder, MahuaDispatch
    order = TicketOrder.objects.create(
        order_ext_no=ext_no, user_id=1, schedule_id=1, cinema_id=1,
        movie_id=1, up_schedule_id='UP1', seats_json='[]', seat_count=1,
        ticket_amount=5000, pay_amount=5000, mobile='13800000000',
        status=TicketOrder.STATUS_WAIT_PICK, pay_status=TicketOrder.PAY_DONE,
        show_start_at=timezone.now() + timedelta(minutes=minutes_before_show),
    )
    MahuaDispatch.objects.create(
        order_ext_no=ext_no, mahua_order_no='MH1',
        request_body={}, dispatch_status=MahuaDispatch.STATUS_TICKETED)
    return order


_MAHUA_CONFIG_OK = {
    'reasons': ['个人原因申请退票', '出错了票', '不可兑换'],
    'disputeMaxCount': 3,
    'disputeCount': 0,
    'evaluateRefundFee': 5,
    'refundRuleConfig': {
        'refundLimitShowTime': 120,
        'refundRuleConfigPut': {
            'startTime': '00:00:00', 'endTime': '23:59:59',
            'refundType': 2,
            'feeAmountEveryTicket': 5,
            'rules': [
                {'minMinutes': '120', 'maxMinutes': None, 'refundable': 'true',
                 'feePerTicket': '5', 'description': '开场前2小时外可退，收5元/张'},
                {'minMinutes': '0', 'maxMinutes': '120', 'refundable': 'false',
                 'feePerTicket': '0', 'description': '开场前2小时内不可退'},
            ],
        },
    },
}


class CanDisputeTests(TestCase):

    def test_wait_pick_beyond_2h_allowed(self):
        """待取票 + 距开场超过 2 小时：可发起纠纷。"""
        order = _make_wait_pick_order(minutes_before_show=181)
        self.assertTrue(can_dispute(order))

    def test_wait_pick_within_2h_blocked(self):
        """待取票 + 距开场不足 2 小时：不可发起（产品策略）。"""
        order = _make_wait_pick_order(minutes_before_show=119)
        self.assertFalse(can_dispute(order))

    def test_dispatching_blocked(self):
        """出票中不走纠纷（走拦截）。"""
        from apps.order.models import TicketOrder
        order = _make_wait_pick_order(minutes_before_show=300)
        order.status = TicketOrder.STATUS_DISPATCHING
        self.assertFalse(can_dispute(order))


class DisputeConfigTests(TestCase):

    def _patch_mahua(self, data=None):
        client = mock.MagicMock()
        client.dispute_reason.return_value = ('000000', data or _MAHUA_CONFIG_OK)
        return mock.patch('apps.upadapter.mahua.MahuaClient', return_value=client)

    def test_config_ok(self):
        """规则内可退：返回原因列表 + 预估手续费（元转分）。"""
        order = _make_wait_pick_order(minutes_before_show=300)
        with self._patch_mahua():
            config = dispute_config(order.id, order.user_id)
        self.assertTrue(config['canApplyRefund'])
        self.assertEqual(config['reasons'], _MAHUA_CONFIG_OK['reasons'])
        self.assertEqual(config['feeEstimate'], 500)

    def test_config_blocked_by_rules(self):
        """麻花 rules 判定当前区间不可退：fail-closed 给出原因。"""
        order = _make_wait_pick_order(minutes_before_show=150)
        # 本地 2 小时放行（150>=120），但麻花 rules [120,180) 判不可退
        data = {
            'reasons': ['个人原因申请退票'], 'disputeMaxCount': 3,
            'disputeCount': 0, 'evaluateRefundFee': 0,
            'refundRuleConfig': {'refundRuleConfigPut': {
                'startTime': '00:00:00', 'endTime': '23:59:59',
                'rules': [
                    {'minMinutes': '180', 'maxMinutes': None, 'refundable': 'true',
                     'feePerTicket': '5', 'description': '开场前3小时外可退'},
                    {'minMinutes': '120', 'maxMinutes': '180', 'refundable': 'false',
                     'feePerTicket': '0', 'description': '开场前3小时内不可退'},
                ]}},
        }
        with self._patch_mahua(data):
            config = dispute_config(order.id, order.user_id)
        self.assertFalse(config['canApplyRefund'])
        self.assertIn('不可退', config['blockReason'])

    def test_config_local_2h_blocked(self):
        """本地 2 小时策略直接拦，不调麻花。"""
        order = _make_wait_pick_order(minutes_before_show=100)
        with self._patch_mahua() as m:
            config = dispute_config(order.id, order.user_id)
        self.assertFalse(config['canApplyRefund'])
        self.assertIn('2小时', config['blockReason'])
        m.dispute_reason.assert_not_called()

    def test_config_max_count_blocked(self):
        """纠纷次数达到上限：不可再发起。"""
        order = _make_wait_pick_order(minutes_before_show=300)
        data = dict(_MAHUA_CONFIG_OK, disputeCount=3)
        with self._patch_mahua(data):
            config = dispute_config(order.id, order.user_id)
        self.assertFalse(config['canApplyRefund'])
        self.assertIn('最大', config['blockReason'])


class ApplyDisputeTests(TestCase):

    def _patch_mahua_apply(self, data=None):
        def _factory(*_args, **_kwargs):
            client = mock.MagicMock()
            client.dispute_reason.return_value = ('000000', _MAHUA_CONFIG_OK)
            client.dispute_apply.return_value = ('000000', data or {
                'disputeId': '321', 'evaluateRefundFee': 5})
            return client
        return mock.patch('apps.upadapter.mahua.MahuaClient', new=_factory)

    def test_apply_ok(self):
        """发起成功：订单 30->90，纠纷/退款单落库，手续费转分。"""
        order = _make_wait_pick_order(ext_no='PP20261007000011')
        with self._patch_mahua_apply():
            from apps.refund.services import apply_dispute
            refund = apply_dispute(order.user_id, order.id,
                                   '个人原因申请退票', '计划有变')
        order.refresh_from_db()
        self.assertEqual(order.status, TicketOrder.STATUS_DISPUTE)
        self.assertEqual(refund.type, Refund.TYPE_DISPUTE)
        self.assertEqual(refund.fee, 500)
        self.assertEqual(refund.refund_amount, 4500)
        dispute = Dispute.objects.get(refund_id=refund.id)
        self.assertEqual(dispute.status, Dispute.STATUS_STARTED)
        self.assertTrue(dispute.up_dispute_no)
        self.assertTrue(DisputeMsg.objects.filter(dispute_id=dispute.id).exists())

    def test_apply_rejects_invalid_reason(self):
        """原因不在麻花 reasons 列表内：拒绝。"""
        order = _make_wait_pick_order(ext_no='PP20261007000012')
        with self._patch_mahua_apply():
            from apps.refund.services import apply_dispute
            with self.assertRaises(BizError):
                apply_dispute(order.user_id, order.id, '编造的原因', '')

    def test_apply_rejects_within_2h(self):
        """距开场不足 2 小时：拒绝发起。"""
        order = _make_wait_pick_order(ext_no='PP20261007000013',
                                      minutes_before_show=100)
        from apps.refund.services import apply_dispute
        with self.assertRaises(BizError):
            apply_dispute(order.user_id, order.id, '个人原因申请退票', '')

    def test_apply_rejects_duplicate(self):
        """已存在进行中的纠纷：拒绝重复发起。"""
        order = _make_wait_pick_order(ext_no='PP20261007000014')
        Dispute.objects.create(order_id=order.id, reason_code='出错了票',
                               status=Dispute.STATUS_STARTED)
        from apps.refund.services import apply_dispute
        with self._patch_mahua_apply():
            with self.assertRaises(BizError):
                apply_dispute(order.user_id, order.id, '个人原因申请退票', '')
