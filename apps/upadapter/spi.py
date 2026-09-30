"""麻花 SPI 接口定义。

统一供应商接口抽象，屏蔽字段差异。每个供应商一个实现。
"""
from abc import ABC, abstractmethod


class MovieChannel(ABC):
    """票务供应商统一接口。"""

    @abstractmethod
    def get_token(self):
        """获取 token（2h 有效，需缓存，严禁每请求都取）。"""

    @abstractmethod
    def get_movies(self, status):
        """影片列表（1热映 2待映）。"""

    @abstractmethod
    def get_cinemas(self, city_code):
        """影院列表。"""

    @abstractmethod
    def get_schedules(self, cinema_id):
        """影院排片（限频，优先回调同步）。"""

    @abstractmethod
    def get_seats_realtime(self, schedule_id):
        """实时座位图（禁止拉取同步，仅现用）。"""

    @abstractmethod
    def dispatch(self, order):
        """放单（下单，从麻花余额扣款）。"""

    @abstractmethod
    def query_order(self, mahua_order_no):
        """放单查询（出票轮询兜底）。"""

    @abstractmethod
    def confirm_receipt(self, mahua_order_no):
        """确认收货。"""

    @abstractmethod
    def intercept(self, mahua_order_no):
        """尝试拦截（未出票取消）。"""

    @abstractmethod
    def urge(self, mahua_order_no):
        """催单。"""

    @abstractmethod
    def dispute(self, order, reason_code):
        """发起纠纷。"""
