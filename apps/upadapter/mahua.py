"""麻花电影客户端（放单模式）。

基于 docs/mahua-api/ 下 32 个接口的正式报文实现，字段映射见 docs/麻花字段级映射.md。

鉴权：所有接口（除登录）通过 header 传 token，MD5 签名 = MD5(bodyJson + key + txntime)。
token 有效期 2h，由 apps.upadapter.token 统一缓存（进程内 LocMem）+ 内置定时器每 30min 刷新
（严禁每请求都取，否则封 IP）。
"""
import hashlib
import json
import logging
import time

import requests
from django.conf import settings

logger = logging.getLogger('app')

# 成功码
SUCCESS_CODE = '000000'
# 拦截失败（转人工，勿直接退款）
INTERCEPT_FAIL_CODE = '100300'
# 10 分钟内已催单
URGE_LIMIT_CODE = '100301'


class MahuaClient:
    """麻花电影 API 客户端（放单模式）。"""

    CHANNEL_ID = 'OP0002'

    def __init__(self):
        cfg = settings.MAHUA
        self.base_url = cfg['BASE_URL'].rstrip('/')
        self.app_key = cfg['APP_KEY']          # 对应 devCode
        self.app_secret = cfg['APP_SECRET']    # 对应签名 key（个人私钥）

    # ---- 底层 ----
    def _sign(self, body_json, txntime):
        """MD5(bodyJson + key + txntime)，UTF-8 编码。"""
        raw = f'{body_json}{self.app_secret}{txntime}'
        return hashlib.md5(raw.encode('utf-8')).hexdigest()

    def _headers(self, body, token=None):
        """构造统一请求 header。body 为 dict 或 None。"""
        body_json = json.dumps(body or {}, ensure_ascii=False, separators=(',', ':'))
        txntime = str(int(time.time() * 1000))
        headers = {
            'channelid': self.CHANNEL_ID,
            'txntime': txntime,
            'devCode': self.app_key,
            'sign': self._sign(body_json, txntime),
            'Content-Type': 'application/json',
        }
        if token:
            headers['token'] = token
        return headers, body_json

    def _post(self, path, body=None, token=None):
        """统一 POST。返回 (rtnCode, rtnData)。"""
        headers, body_json = self._headers(body, token=token)
        url = f'{self.base_url}{path}'
        resp = requests.post(url, data=body_json.encode('utf-8'), headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get('rtnCode'), data.get('rtnData')

    def _ok(self, code):
        return code == SUCCESS_CODE

    # ---- 通用 / 账户 ----
    def fetch_token(self):
        """获取 token（登录接口，无需 token）。返回 (token, userName)。"""
        code, data = self._post('/api/user-server/user/dev/login', body={})
        if not self._ok(code):
            logger.error('麻花获取token失败: %s', data)
            return '', ''
        data = data or {}
        return data.get('token', ''), data.get('userName', '')

    def account_info(self, token):
        """麻花账户信息（余额镜像）。返回 rtnData（含 balanceNew/frozenAmountNew）。"""
        return self._post('/api/movie-server/movie/put/account/detail', body={}, token=token)

    def submit_recharge(self, token, order_no):
        """提交充值流水。orderNo=28/32 位。"""
        return self._post('/api/movie-server/movie/user/recharge/new', {'orderNo': order_no}, token=token)

    def recharge_records(self, token, now_id=None):
        """充值记录查询（分页，每页 10 条）。"""
        body = {'nowId': now_id} if now_id is not None else {}
        return self._post('/api/movie-server/movie/user/recharge/infos', body, token=token)

    # ---- 基础信息 / 电影 ----
    def get_city_code(self, token):
        """城市代码（数组：cityId/cityPinyin/cityName）。"""
        return self._post('/api/movie-server/movie/info/city', body={}, token=token)

    def get_hot_movies(self, token, city_id):
        """热映电影（一次性全量）。ci=城市id 必填。"""
        return self._post('/api/movie-server/movie/info/movieOnInfoList', {'ci': city_id}, token=token)

    def get_coming_movies(self, token, page=1):
        """待映电影（分页）。路径/参数以正式报文为准：comingList + pageNum。"""
        return self._post('/api/movie-server/movie/info/comingList', {'pageNum': page}, token=token)

    def get_movie_detail(self, token, film_id):
        """电影详情。"""
        return self._post('/api/movie-server/movie/info/movieInfo', {'filmId': film_id}, token=token)

    # ---- 基础信息 / 影院 ----
    def get_cinema_list(self, token, city_id, **filters):
        """影院列表。ci 必填，其余筛选可选。"""
        body = {'ci': city_id}
        body.update({k: v for k, v in filters.items() if v is not None})
        return self._post('/api/movie-server/movie/info/cinemaList', body, token=token)

    def get_cinema_regions(self, token, city_id, date=None, film_id=None):
        """影院区域数量（「全城▾」筛选项）。ci 必填，date/film_id 选填。

        返回 rtnData 为 [{regionName, num}] 列表；仅传 ci 即可拿到全市所有区。
        传 film_id 则收敛为「有该片排片的区」，用于影片搜索/详情场景。
        """
        body = {'ci': int(city_id)}
        if date:
            body['date'] = date
        if film_id is not None:
            body['filmId'] = film_id
        return self._post('/api/movie-server/movie/info/filterCinemas/region', body, token=token)

    def get_cinema_brands(self, token, city_id, date=None, film_id=None):
        """影院品牌数量（「品牌▾」筛选项）。ci 必填，date/film_id 选填。

        返回 rtnData 为 [{brandName, num}] 列表；传 film_id 则收敛为「有该片排片的品牌」。
        """
        body = {'ci': int(city_id)}
        if date:
            body['date'] = date
        if film_id is not None:
            body['filmId'] = film_id
        return self._post('/api/movie-server/movie/info/filterCinemas/brand', body, token=token)

    def get_cinema_detail(self, token, cinema_id):
        """影院详情。"""
        return self._post('/api/movie-server/movie/info/cinemaInfo', {'cinemaId': cinema_id}, token=token)

    def get_schedule(self, token, cinema_id):
        """影院排片（限频 150/min，优先用回调同步）。正式路径 /info/cinema/schedule。"""
        return self._post('/api/movie-server/movie/info/cinema/schedule', {'cinemaId': int(cinema_id)}, token=token)

    def get_schedule_detail(self, token, show_id):
        """排片详情（实时，禁止拉取同步）。"""
        return self._post('/api/movie-server/movie/info/schedule/detail', {'showId': show_id}, token=token)

    def get_seats_realtime(self, token, show_id):
        """座位情况（实时，禁止拉取同步，限频 60/min）。"""
        return self._post('/api/movie-server/movie/info/cinema/show/seats', {'showId': show_id}, token=token)

    # ---- OCR 图片识别估价（放单模式）----
    def ocr_evaluate_price(self, token, img_base64):
        """影院图片识别报价。img_base64 不含 data:image 前缀，≤5M。

        0.01 元/次（成功返回估价才扣费）；限频 60/min 由调用方令牌桶控制（见 ocr_services）。
        imgBase64 进 JSON 签名体（与放单普通接口同，≠接单模式图片 multipart 不签名）。
        超时放宽到 20s（OCR 比普通接口慢）。返回 (rtnCode, rtnMsg, rtnData 归一化对象)
        ——额外带出 rtnMsg 供上层把业务失败文案归一化成用户提示。
        """
        headers, body_json = self._headers({'imgBase64': img_base64}, token=token)
        url = f'{self.base_url}/api/movie-server/movie/ocr/evaluate/price'
        t0 = time.time()
        resp = requests.post(url, data=body_json.encode('utf-8'), headers=headers, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        code = data.get('rtnCode')
        msg = data.get('rtnMsg')
        rdata = self._as_obj(data.get('rtnData'))
        logger.info('mahua.ocr code=%s cost=%dms size=%d',
                    code, int((time.time() - t0) * 1000), len(img_base64))
        return code, msg, rdata

    @staticmethod
    def _as_obj(v):
        """rtnData 兼容：线上可能已是 dict/list，也可能是 JSON 字符串（文档称字符串）。"""
        if v is None or isinstance(v, (dict, list)):
            return v
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:  # noqa: BLE001
                return v
        return v

    # ---- 放单 ----
    def dispatch(self, token, payload):
        """放单（下单）。payload 见字段映射 §4.1。返回 rtnData=放单号字符串。"""
        return self._post('/api/movie-server/movie/put/add', payload, token=token)

    def query_order(self, token, out_id):
        """放单查询。outId=外部单号。"""
        return self._post('/api/movie-server/movie/put/query', {'outId': out_id}, token=token)

    def order_list(self, token, tag, now_id=None, out_id=None, put_order_id=None):
        """放单列表（对账/兜底，每页 10 条）。"""
        body = {'tag': tag}
        if now_id is not None:
            body['nowId'] = now_id
        if out_id:
            body['outId'] = out_id
        if put_order_id:
            body['putOrderId'] = put_order_id
        return self._post('/api/movie-server/movie/put/in/list', body, token=token)

    def confirm_receipt(self, token, out_id):
        """确认收货。"""
        return self._post('/api/movie-server/movie/put/confirm', {'outId': out_id}, token=token)

    def intercept(self, token, out_id):
        """尝试拦截（未出票取消）。100300=失败转人工。"""
        return self._post('/api/movie-server/movie/put/tryIntercept', {'outId': out_id}, token=token)

    def urge(self, token, out_id):
        """催单（仅特惠模式）。100301=10 分钟内已催。"""
        return self._post('/api/movie-server/movie/put/pressOrder', {'outId': out_id}, token=token)

    # ---- 纠纷 ----
    def dispute_reason(self, token, out_id=None, cinema_id=None, standard_id=None):
        """纠纷原因/退票规则。"""
        body = {}
        if out_id:
            body['outId'] = out_id
        if cinema_id is not None:
            body['cinemaId'] = cinema_id
        if standard_id:
            body['standerId'] = standard_id
        return self._post('/api/movie-server/movie/put/dispute/config', body, token=token)

    def dispute_apply(self, token, out_id, reason, content, out_dispute_id=None, call_back_url=None):
        """发起纠纷。disputeReason 2026-01-26 起必传。"""
        body = {'outId': out_id, 'disputeReason': reason, 'content': content}
        if out_dispute_id:
            body['outDisputeId'] = out_dispute_id
        if call_back_url:
            body['callBackUrl'] = call_back_url
        return self._post('/api/movie-server/movie/put/dispute', body, token=token)

    def dispute_cancel(self, token, dispute_id=None, out_dispute_id=None):
        """取消纠纷（确认收货前）。二选一。"""
        body = {}
        if dispute_id is not None:
            body['disputeId'] = dispute_id
        if out_dispute_id:
            body['outDisputeId'] = out_dispute_id
        return self._post('/api/movie-server/movie/put/in/dispute/cancelBeforeComplete', body, token=token)

    def dispute_reply(self, token, content, dispute_id=None, out_dispute_id=None):
        """纠纷回复。content 必填。"""
        body = {'content': content}
        if dispute_id:
            body['disputeId'] = dispute_id
        if out_dispute_id:
            body['outDisputeId'] = out_dispute_id
        return self._post('/api/movie-server/movie/put/in/dispute/reply', body, token=token)
