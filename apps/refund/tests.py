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
from apps.refund.models import Refund
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
