"""独立测试脚本：直连麻花 movieOnInfoList，打印请求头/入参/响应。

用法：
    python3 scripts/test_mahua_hot_movies.py [城市cityId，默认 8=成都]

凭据从 ticket/.env 读取（MAHUA_BASE_URL / MAHUA_APP_KEY / MAHUA_APP_SECRET）。
签名算法：sign = MD5(bodyJson + APP_SECRET + txntime)，bodyJson 为
json.dumps(body, ensure_ascii=False, separators=(',', ':')) 的紧凑串。
"""
import hashlib
import json
import os
import re
import sys
import time

import requests

TICKET_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env():
    env = {}
    with open(os.path.join(TICKET_DIR, '.env'), encoding='utf-8') as f:
        for line in f:
            m = re.match(r'^([A-Z_]+)=(.*)$', line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return env


def compact(body):
    """与 apps/upadapter/mahua.py 保持一致的紧凑序列化（签名输入必须一致）。"""
    return json.dumps(body or {}, ensure_ascii=False, separators=(',', ':'))


def build_headers(body, app_key, app_secret, token=None):
    body_json = compact(body)
    txntime = str(int(time.time() * 1000))
    sign = hashlib.md5(f'{body_json}{app_secret}{txntime}'.encode('utf-8')).hexdigest()
    headers = {
        'channelid': 'OP0002',
        'txntime': txntime,
        'devCode': app_key,
        'sign': sign,
        'Content-Type': 'application/json',
    }
    if token:
        headers['token'] = token
    return headers, body_json


def post(url, body, app_key, app_secret, token=None):
    headers, body_json = build_headers(body, app_key, app_secret, token)
    print(f'\n===== POST {url}')
    print('----- 请求头 -----')
    for k, v in headers.items():
        print(f'{k}: {v}')
    print('----- 请求体 -----')
    print(body_json)
    resp = requests.post(url, data=body_json.encode('utf-8'), headers=headers, timeout=15)
    data = resp.json()
    print('----- 响应 -----')
    print('rtnCode:', data.get('rtnCode'))
    return data


def main():
    city_id = sys.argv[1] if len(sys.argv) > 1 else '8'
    env = load_env()
    base_url = env['MAHUA_BASE_URL'].rstrip('/')
    app_key = env['MAHUA_APP_KEY']
    app_secret = env['MAHUA_APP_SECRET']

    # 1) 登录换 token（登录接口本身不需要 token）
    login = post(f'{base_url}/api/user-server/user/dev/login', {}, app_key, app_secret)
    token = (login.get('rtnData') or {}).get('token', '')
    print('token:', token[:20] + '...' if token else '(空！后续调用会失败)')
    if not token:
        sys.exit(1)

    # 2) 热映列表 movieOnInfoList，入参只有 ci（城市id）
    data = post(
        f'{base_url}/api/movie-server/movie/info/movieOnInfoList',
        {'ci': int(city_id)},
        app_key, app_secret, token=token,
    )

    films = data.get('rtnData') or []
    if isinstance(films, str):
        films = json.loads(films)
    print(f'\n===== 摘要：ci={city_id} 返回 {len(films)} 部在映影片')
    keyword = '年会不能停'
    hits = [f for f in films if keyword in (f.get('name') or '')]
    for f in hits:
        print(f'----- 「{keyword}」原始报文 -----')
        print(json.dumps(f, ensure_ascii=False, indent=2))
    if not hits:
        print(f'（返回中无「{keyword}」）')
        # 打印前 3 部片名供核对
        for f in films[:3]:
            print('样例:', f.get('id'), f.get('name'), f.get('publishTime'))


if __name__ == '__main__':
    main()
