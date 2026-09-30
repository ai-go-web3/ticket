"""只读探测：验证麻花「影院列表→影院排片→座位情况」真实数据链路。

放单(dispatch)本身要扣真钱，本命令不调用放单，只走通到座位为止，
确认能拿到真实 cinemaId / showId / seatId / row / col / price —— 这些正是
build_dispatch_payload 需要的字段来源。

用法：
    python manage.py probe_dispatch_chain --city 8 --limit 1 --settings=config.settings_dev
"""
import json

from django.core.management.base import BaseCommand

from apps.upadapter.mahua import MahuaClient, SUCCESS_CODE


class Command(BaseCommand):
    help = '只读探测麻花影院→排片→座位链路（不放单、不花钱）'

    def add_arguments(self, parser):
        parser.add_argument('--city', default='8', help='麻花 cityId，成都=8')
        parser.add_argument('--limit', type=int, default=1, help='探测影片数量')
        parser.add_argument('--film', default='', help='指定 filmId（跳过取热映）')

    def handle(self, *a, **opts):
        city = opts['city']
        client = MahuaClient()

        code, token_data = client._post('/api/user-server/user/dev/login', body={})
        if code != SUCCESS_CODE:
            self.stderr.write(f'登录失败 {code} {token_data}')
            return
        token = (token_data or {}).get('token', '')
        self.stdout.write(self.style.SUCCESS(f'[1] 登录 OK，token={token[:12]}...'))

        # 选一部热映影片
        film_id = opts['film']
        film_name = ''
        if not film_id:
            code, data = client.get_hot_movies(token, city)
            movies = data if isinstance(data, list) else (data or {}).get('list') or (data or {}).get('movieOnInfos') or []
            if not movies:
                self.stderr.write(f'热映为空 {code} {json.dumps(data, ensure_ascii=False)[:300]}')
                return
            self.stdout.write(f'[2] 热映 {len(movies)} 部，字段样例: {list(movies[0].keys())}')
            for m in movies[:opts['limit']]:
                film_id = str(m.get('filmId') or m.get('id') or '')
                film_name = m.get('filmName') or m.get('name') or ''
                self.stdout.write(f'    取影片: filmId={film_id} name={film_name}')
                self._probe_films(client, token, city, film_id, film_name)
        else:
            self._probe_films(client, token, city, film_id, film_name)

    def _probe_films(self, client, token, city, film_id, film_name):
        # 影院列表
        code, data = client.get_cinema_list(token, city, filmId=film_id)
        cinemas = data if isinstance(data, list) else (data or {}).get('list') or (data or {}).get('cinemaList') or []
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n[3] 影院列表 film={film_name or film_id} code={code} 命中 {len(cinemas)} 家'))
        if not cinemas:
            self.stdout.write(f'    样例返回: {json.dumps(data, ensure_ascii=False)[:400]}')
            return
        c0 = cinemas[0]
        self.stdout.write(f'    影院字段: {list(c0.keys())}')
        cinema_id = str(c0.get('cinemaId') or c0.get('id') or '')
        cinema_name = c0.get('cinemaName') or c0.get('name') or ''
        self.stdout.write(f'    取影院: cinemaId={cinema_id} name={cinema_name}')

        # 影院排片
        code, data = client.get_schedule(token, cinema_id)
        schedules = data if isinstance(data, list) else (data or {}).get('list') or (data or {}).get('scheduleList') or []
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n[4] 影院排片 cinema={cinema_name} code={code} 场次 {len(schedules)}'))
        if not schedules:
            self.stdout.write(f'    样例返回: {json.dumps(data, ensure_ascii=False)[:400]}')
            return
        # 优先挑本片的一个场次
        target = None
        for s in schedules:
            if str(s.get('filmId') or s.get('movieId') or '') == str(film_id):
                target = s
                break
        target = target or schedules[0]
        self.stdout.write(f'    场次字段: {list(target.keys())}')
        show_id = str(target.get('showId') or target.get('id') or '')
        self.stdout.write(f'    取场次: showId={show_id} '
                          f'{target.get("showTime") or target.get("startTime") or ""} '
                          f'{target.get("hallName") or target.get("screenName") or ""}')

        # 座位情况
        code, data = client.get_seats_realtime(token, show_id)
        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\n[5] 座位情况 showId={show_id} code={code}'))
        if not isinstance(data, (list, dict)):
            self.stdout.write(f'    rtnData 非结构: {str(data)[:200]}')
            return
        seats = data.get('seats') if isinstance(data, dict) else data
        if isinstance(seats, dict):
            seats = seats.get('list') or list(seats.values())
        self.stdout.write(f'    座位容器字段: {list(data.keys()) if isinstance(data, dict) else "(list)"}')
        sample = (seats or [])[:5]
        for s in sample:
            self.stdout.write(f'      seat: {json.dumps(s, ensure_ascii=False)[:220]}')
        self.stdout.write(self.style.SUCCESS(
            f'\n链路 OK：filmId={film_id} cinemaId={cinema_id} showId={show_id} '
            f'座位样例 {len(sample)} 个 —— 放单 payload 所需的 showId/row/col/price 均可取到。'))
