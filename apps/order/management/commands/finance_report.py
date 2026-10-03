"""财务日报（对账/盈利）：python manage.py finance_report [--days 7] [--date YYYY-MM-DD]"""
from django.core.management.base import BaseCommand

from apps.order.reporting import daily_report


class Command(BaseCommand):
    help = '按日汇总实收/票款/成本/毛利/对账差异（口径见 docs/财务口径与对账.md）'

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=7, help='最近 N 天（含今天），默认 7')
        parser.add_argument('--date', type=str, default=None, help='只看指定日期 YYYY-MM-DD')

    def handle(self, *args, **opts):
        rep = daily_report(days=opts['days'], date=opts['date'])
        self.stdout.write('%-11s %5s %10s %10s %10s %10s %10s %10s %8s %s' % (
            '日期', '单数', '实收', '票款', '预估成本', '实际成本', '预估毛利', '实际毛利', '差异', '退款单'))
        for d in rep['days']:
            self.stdout.write('%-11s %5d %10.2f %10.2f %10.2f %10.2f %10.2f %10.2f %8.2f %s' % (
                d['date'], d['orders'], d['gmv'], d['ticket'], d['est_cost'],
                d['settle'], d['est_profit'], d['real_profit'], d['diff'],
                f"{d['refunded_orders']}单/{d['refunded_amount']}元"))
        alerts = rep['alerts']
        if alerts['unsettled']:
            self.stdout.write(self.style.WARNING('缺结算价单: %s' % ', '.join(alerts['unsettled'][:10])))
        if alerts['inverted']:
            self.stdout.write(self.style.ERROR('倒挂单(结算>票款): %s' % ', '.join(alerts['inverted'][:10])))
