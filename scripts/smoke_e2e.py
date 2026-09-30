"""端到端冒烟测试：登录→锁座→建单→模拟支付→订单查询→座位图。

用 Django test client + 独立临时库（ticket_smoke）跑，不污染真实库。
运行：MYSQL_DATABASE=ticket_smoke python manage.py shell < scripts/smoke_e2e.py
"""
import os
import sys
from datetime import timedelta

import django

# 确保 ticket 目录在 sys.path（manage.py 所在目录）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
os.environ['MYSQL_DATABASE'] = 'ticket_smoke'
django.setup()

from django.test import Client
from django.utils import timezone
from apps.catalog.models import City, Movie, Cinema, Schedule
from apps.auths.models import AppUser
from apps.order.models import TicketOrder
from apps.catalog.city_mapping import std_to_mahua

PASS = []
FAIL = []


def check(name, cond, detail=''):
    if cond:
        PASS.append(name)
        print(f'  [PASS] {name}')
    else:
        FAIL.append(name)
        print(f'  [FAIL] {name} {detail}')


def seed():
    """灌演示数据：城市/影片/影院/排期。"""
    City.objects.update_or_create(city_code='8', defaults={'city_name': '成都', 'std_code': '510100', 'pinyin': 'chengdu', 'hot': 1})
    movie, _ = Movie.objects.get_or_create(
        up_movie_id='mv_001', defaults={'name': '流浪地球3', 'status': 1, 'duration': 165, 'rating': 9.2, 'type': '科幻'},
    )
    cinema, _ = Cinema.objects.get_or_create(
        up_cinema_id='cn_001', defaults={'name': 'CGV影城测试店', 'city_code': '8', 'address': '测试路1号'},
    )
    now = timezone.now()
    schedule, _ = Schedule.objects.update_or_create(
        up_schedule_id='sh_001', defaults={
            'cinema_id': cinema.id, 'movie_id': movie.id, 'hall_name': '10号厅',
            # 造一个「未来可售」场次：3 小时后开场，特惠停售线 = 开场前 30 分钟
            'start_at': now + timedelta(hours=3),
            'stopsell_at': now + timedelta(hours=2, minutes=30),
            'min_price': 4500, 'sell_status': 1, 'snapshot_at': now,
        },
    )
    return movie, cinema, schedule


def main():
    print('=== 端到端冒烟测试 ===')
    client = Client()

    # 0. seed
    movie, cinema, schedule = seed()
    print(f'[seed] movie={movie.id} cinema={cinema.id} schedule={schedule.id}')

    # 1. 登录（dev 降级：WX_APPID 已配置，会走真实 code2session，此处直接造用户+token）
    #    真实 code2session 需要有效 code，冒烟用直接签发 token 模拟登录
    user, _ = AppUser.objects.get_or_create(
        openid='smoke_openid', defaults={'nickname': '冒烟用户', 'unionid': None},
    )
    from apps.auths.authentication import gen_token
    token = gen_token(user.id)
    auth = {'HTTP_AUTHORIZATION': f'Bearer {token}'}
    print(f'[login] user={user.id} token={token[:20]}...')

    # 2. 座位图查询
    resp = client.get(f'/api/v1/catalog/schedules/{schedule.id}/seats')
    data = resp.json()
    check('座位图查询', resp.status_code == 200 and data['code'] == 0, str(data))
    check('座位图结构', data['data']['rows'] and 'seats' in data['data']['rows'][0], str(data)[:200])
    check('座位含name/price', 'name' in data['data']['rows'][0]['seats'][0] and 'price' in data['data']['rows'][0]['seats'][0])
    seat_row = data['data']['rows'][0]
    price = seat_row['seats'][0]['price']

    # 找一个空座
    empty_seat = None
    for r in data['data']['rows']:
        for s in r['seats']:
            if s['status'] == 0:
                empty_seat = {'row': r['row'], 'col': s['col'], 'name': s['name'], 'price': s['price']}
                break
        if empty_seat:
            break
    check('存在空座', empty_seat is not None, str(empty_seat))

    # 3. 锁座
    resp = client.post('/api/v1/seat/lock', data={
        'scheduleId': schedule.id, 'seats': [empty_seat],
    }, content_type='application/json', **auth)
    data = resp.json()
    check('锁座成功', data['code'] == 0 and 'lockToken' in data['data'], str(data))
    lock_token = data['data']['lockToken']

    # 4. 建单
    resp = client.post('/api/v1/order/', data={
        'lockToken': lock_token, 'scheduleId': schedule.id,
        'seats': [empty_seat], 'mobile': '13888880000',
    }, content_type='application/json', **auth)
    data = resp.json()
    check('建单成功', data['code'] == 0 and 'id' in data['data'], str(data))
    order_id = data['data']['id']
    check('订单金额为分', data['data']['pay_amount'] == 4800, f"pay_amount={data['data']['pay_amount']}")  # 4500+300

    # 5. 模拟支付（开发降级）
    resp = client.post('/api/v1/pay/mock', data={'orderId': order_id}, content_type='application/json', **auth)
    data = resp.json()
    check('模拟支付成功', data['code'] == 0, str(data))

    # 6. 订单详情（验证状态迁移 + 序列化展示字段）
    resp = client.get(f'/api/v1/order/{order_id}', **auth)
    data = resp.json()
    check('订单详情查询', data['code'] == 0, str(data))
    o = data['data']
    check('订单状态已出票中', o['status'] == TicketOrder.STATUS_DISPATCHING, f"status={o['status']}")
    check('含movieName', o.get('movieName') == '流浪地球3', f"movieName={o.get('movieName')}")
    check('含cinemaName', o.get('cinemaName') == 'CGV影城测试店', f"cinemaName={o.get('cinemaName')}")
    check('含statusText', o.get('statusText') == '出票中', f"statusText={o.get('statusText')}")
    check('seats为名称列表', o.get('seats') == [empty_seat['name']], f"seats={o.get('seats')}")

    # 7. 我的订单列表
    resp = client.get('/api/v1/order/list', **auth)
    data = resp.json()
    check('订单列表查询', data['code'] == 0 and len(data['data']) >= 1, str(data))

    # 8. 重复锁同一座位应失败（防超卖）
    resp = client.post('/api/v1/seat/lock', data={
        'scheduleId': schedule.id, 'seats': [empty_seat],
    }, content_type='application/json', **auth)
    data = resp.json()
    # 座位已被当前用户下单消费，锁应失败或被占用
    check('防超卖（重复锁座被拦截）', data['code'] != 0, str(data))

    print(f'\n=== 结果：{len(PASS)} 通过 / {len(FAIL)} 失败 ===')
    if FAIL:
        print('失败项：', FAIL)
        raise SystemExit(1)


if __name__ == '__main__':
    main()
