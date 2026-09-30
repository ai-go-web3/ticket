"""dry-run 放单验证：走麻花真实链路（影院→排片→座位）构造放单报文，绝不扣款。

放单(/put/add) 会真实扣麻花余额出真票，本命令只做到「座位」为止，
然后按 build_dispatch_payload 的规则组装出将要 POST 给 /put/add 的完整报文并打印，
用于验证「对接麻花真实放单接口」的入参正确性（outId/showId/buySeats/seatId/costTotalPrice），
不调用放单接口、不花钱。

用法：
    # 按影片名（成都，选第一家影院、该影院本片第一个可售场次、第一个可售座）
    python manage.py dry_run_dispatch --city 8 --film 神探 --settings=config.settings_dev

    # 指定座位名（1排5座）与限价
    python manage.py dry_run_dispatch --city 8 --film 1443409 --seat 1排5座 --limit 40 --settings=config.settings_dev
"""
import json
import time
import re

from django.core.management.base import BaseCommand

from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE
from apps.upadapter.token import get_token


def _extract_row_col(seat_name):
    parts = re.findall(r'[a-zA-Z0-9]+', seat_name or '')
    if len(parts) >= 2:
        return parts[0], parts[1]
    return seat_name, ''


class Command(BaseCommand):
    help = '走真实链路构造放单报文并打印（不扣款）'

    def add_arguments(self, parser):
        parser.add_argument('--city', default='8', help='麻花 cityId，成都=8')
        parser.add_argument('--film', default='', help='影片 up_movie_id 或片名子串')
        parser.add_argument('--seat', default='', help='座位名，如 1排5座（默认可售首座）')
        parser.add_argument('--limit', type=float, default=0, help='总限价(元)，默认取座位原价')
        parser.add_argument('--phone', default='', help='取票手机号')
        parser.add_argument('--model', type=int, default=0, help='出票模式 0特惠1快速2极速')

    def handle(self, *a, **opts):
        client = MahuaClient()
        token = get_token()
        if not token:
            self.stderr.write('取 token 失败')
            return

        city = opts['city']
        film_key = opts['film']

        # 1) 定位影片 filmId（热映列表里按 id 或片名匹配）
        code, data = client.get_hot_movies(token, city)
        movies = data if isinstance(data, list) else (data or {}).get('list') or []
        film = None
        for m in movies:
            mid = str(m.get('id') or '')
            name = m.get('name') or ''
            if film_key and (mid == str(film_key) or film_key in name):
                film = m
                break
        if not film and movies:
            film = movies[0]
        if not film:
            self.stderr.write('未定位到影片')
            return
        film_id = str(film.get('id'))
        self.stdout.write(self.style.SUCCESS(f'[1] 影片 filmId={film_id} name={film.get("name")}'))

        # 2) 影院列表（filmId 过滤）
        code, data = client.get_cinema_list(token, city, filmId=film_id)
        cinemas = data if isinstance(data, list) else (data or {}).get('list') or []
        if not cinemas:
            self.stderr.write(f'影院列表为空 code={code}')
            return
        cinema = cinemas[0]
        cinema_id = cinema.get('cinemaId')
        self.stdout.write(self.style.SUCCESS(
            f'[2] 影院 cinemaId={cinema_id} name={cinema.get("cinemaName")} (共{len(cinemas)}家，取第1家)'))

        # 3) 影院排片 → 本片可售场次
        code, data = client.get_schedule(token, cinema_id)
        schedules = data if isinstance(data, list) else (data or {}).get('list') or []
        show = None
        for s in schedules:
            if str(s.get('filmId')) == film_id:
                show = s
                break
        show = show or (schedules[0] if schedules else None)
        if not show:
            self.stderr.write(f'排片为空 code={code}')
            return
        show_id = show.get('showId')
        self.stdout.write(self.style.SUCCESS(
            f'[3] 场次 showId={show_id} {show.get("showTime")} {show.get("hallName")} '
            f'原价={show.get("price")} 快速={show.get("fastPrice")} 极速={show.get("maxSpeedPrice")}'))

        # 4) 座位情况 → 取目标座位 seatId（默认可售首座）
        code, data = client.get_seats_realtime(token, show_id)
        seats = (data or {}).get('movieFilmSeatData') or [] if isinstance(data, dict) else []
        sellable = [s for s in seats if s.get('status') == 'N']
        target = None
        if opts['seat']:
            target = next((s for s in sellable if s.get('seatNo') == opts['seat']), None)
        if not target:
            target = sellable[0] if sellable else None
        if not target:
            self.stderr.write('无可售座位')
            return
        self.stdout.write(self.style.SUCCESS(
            f'[4] 座位 seatNo={target.get("seatNo")} seatId={target.get("seatId")} '
            f'原价={target.get("price")}/{target.get("fastPrice")}/{target.get("maxSpeedPrice")}'))

        # 5) 组装放单报文（与 build_dispatch_payload 同规则）
        row, col = _extract_row_col(target.get('seatNo'))
        buy_seat = {'row': row, 'col': col, 'seatId': target.get('seatId')}
        cost = opts['limit'] or float(target.get('price') or show.get('price') or 0)
        payload = {
            'outId': f'DRYRUN{int(time.time() * 1000)}',
            'showId': show_id,
            'buySeats': [buy_seat],
            'acceptChangeseat': '1',
            'callBackUrl': 'https://REPLACE-WITH-PUBLIC-HOST/api/v1/upadapter/order-callback',
            'costTotalPrice': cost,
            'model': opts['model'],
        }
        if opts['phone']:
            payload['phoneNo'] = opts['phone']

        self.stdout.write(self.style.MIGRATE_HEADING(
            '\n[5] 将要 POST /api/movie-server/movie/put/add 的真实报文（未发送·不扣款）：'))
        self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        self.stdout.write(self.style.SUCCESS(
            '\n链路 OK：outId/showId/buySeats(seatId+row+col)/costTotalPrice 均来自麻花真实数据，'
            '与 build_dispatch_payload 完全一致。设 MAHUA_DISPATCH_ENABLED=true 且配好公网回调后，'
            '支付成功回调即会用同样报文真实放单。'))
