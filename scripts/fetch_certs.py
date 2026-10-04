"""容器启动时从微信云托管对象存储（COS）拉取微信商户 API 证书。

背景：
- 云托管容器文件系统是临时的，重启/扩缩容即还原，证书不能靠「一次性拷入」持久保存；
- 商户 API 证书（apiclient_cert.pem / apiclient_key.pem）是退款双向 TLS 所必需，
  但私钥严禁入库/进镜像，早期通过环境变量注入 PEM 文本（易被控制台把换行弄坏，
  导致退款报 `SSLError(524297, '[SSL] PEM lib')`）。

方案（替代环境变量注入）：
- 证书文件先上传到微信云托管「对象存储」（控制台可视化上传，保留原始换行）；
- 容器启动时在容器内调用开放接口服务 `http://api.weixin.qq.com/_/cos/getauth`
  获取临时密钥（该接口只能在容器内调用，识别云托管身份），再用 COS SDK 下载证书到本地。

依赖：cos-python-sdk-v5（见 requirements.txt）。

所需环境变量（在云托管「服务设置 → 环境变量」配置）：
- WX_COS_REGION     对象存储地域，如 ap-shanghai
- WX_COS_BUCKET     对象存储桶名（含 appid 后缀），如 7072-prod-xxxx-125xxxx
- WX_COS_CERT_KEY   商户证书在桶内的对象键，如 certs/apiclient_cert.pem
- WX_COS_KEY_KEY    商户私钥在桶内的对象键，如 certs/apiclient_key.pem
- WX_MCH_CERT_PATH  证书落地路径（默认 /app/certs/apiclient_cert.pem）
- WX_MCH_KEY_PATH   私钥落地路径（默认 /app/certs/apiclient_key.pem）

权限前置：云托管「微信令牌权限配置」需添加 `/_/cos/getauth`，否则取临时密钥会失败。

失败即退出（fail-fast）：证书拉取/校验失败直接 exit 1，让部署失败立即暴露，
而不是静默降级为「骨架退款」把问题留到线上退款时才炸。
"""
import os
import subprocess
import sys

import requests

GETAUTH_URL = 'http://api.weixin.qq.com/_/cos/getauth'


def log(msg):
    print(f'[fetch_certs] {msg}', flush=True)


def fail(msg):
    log(f'错误：{msg}')
    sys.exit(1)


def get_tmp_cred():
    """容器内调开放接口服务拿对象存储临时密钥（识别云托管身份）。"""
    try:
        resp = requests.get(GETAUTH_URL, timeout=15)
    except requests.RequestException as exc:
        fail(f'获取对象存储临时密钥失败（网络/开放接口服务异常）：{exc}')

    if resp.status_code != 200:
        fail(
            f'获取对象存储临时密钥失败（HTTP {resp.status_code}）：'
            f'请确认已在「微信令牌权限配置」添加 /_/cos/getauth 权限。'
        )
    try:
        data = resp.json()
    except ValueError:
        fail(f'临时密钥响应非 JSON：{resp.text[:200]}')

    secret_id = data.get('TmpSecretId') or data.get('tmpSecretId') or data.get('SecretId')
    secret_key = data.get('TmpSecretKey') or data.get('tmpSecretKey') or data.get('SecretKey')
    token = data.get('Token') or data.get('token') or data.get('SessionToken')
    if not (secret_id and secret_key and token):
        fail(f'临时密钥响应字段缺失：{sorted(data.keys())}')
    return secret_id, secret_key, token


def build_client(region, secret_id, secret_key, token):
    try:
        from qcloud_cos import CosConfig, CosS3Client
    except ImportError:
        fail('缺少依赖 cos-python-sdk-v5，请确认已加入 requirements.txt 并重新构建镜像。')

    config = CosConfig(
        Region=region,
        SecretId=secret_id,
        SecretKey=secret_key,
        Token=token,
        Scheme='https',
    )
    return CosS3Client(config)


def download(client, bucket, obj_key, dest):
    """下载单个对象并写入 dest，返回写入字节数。"""
    try:
        resp = client.get_object(Bucket=bucket, Key=obj_key)
    except Exception as exc:  # noqa: BLE001 COS SDK 抛出的异常类型不统一
        fail(f'下载 {obj_key} 失败：{exc}')
    body = resp['Body'].get_raw_stream().read()
    if not body:
        fail(f'下载 {obj_key} 得到空内容，请检查对象键是否正确、文件是否已上传。')
    with open(dest, 'wb') as f:
        f.write(body)
    return len(body)


def verify(cert_file, key_file):
    """openssl 落盘自检：证书可解析、私钥可校验、证书与私钥公钥一致。"""
    checks = [
        (['openssl', 'x509', '-in', cert_file, '-noout', '-subject'], '证书解析'),
        (['openssl', 'pkey', '-in', key_file, '-noout', '-check'], '私钥校验'),
    ]
    for cmd, label in checks:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            fail(f'{label}失败：{r.stderr.strip()[:200]}')

    # 用「公钥」比对证书与私钥是否同一套（比 -modulus 更通用：兼容 PKCS#8
    # `BEGIN PRIVATE KEY` 私钥，其 -modulus 无输出，会导致误判）。
    cert_pub = subprocess.run(
        ['openssl', 'x509', '-in', cert_file, '-noout', '-pubkey'],
        capture_output=True, text=True,
    ).stdout
    key_pub = subprocess.run(
        ['openssl', 'pkey', '-in', key_file, '-pubout'],
        capture_output=True, text=True,
    ).stdout
    if not cert_pub or not key_pub or cert_pub != key_pub:
        fail('证书与私钥不匹配（公钥不一致），请确认上传的是同一套证书。')


def main():
    region = os.environ.get('WX_COS_REGION', '').strip()
    bucket = os.environ.get('WX_COS_BUCKET', '').strip()
    cert_key = os.environ.get('WX_COS_CERT_KEY', '').strip()
    key_key = os.environ.get('WX_COS_KEY_KEY', '').strip()

    cert_file = os.environ.get('WX_MCH_CERT_PATH', '/app/certs/apiclient_cert.pem')
    key_file = os.environ.get('WX_MCH_KEY_PATH', '/app/certs/apiclient_key.pem')

    missing = [n for n, v in (
        ('WX_COS_REGION', region),
        ('WX_COS_BUCKET', bucket),
        ('WX_COS_CERT_KEY', cert_key),
        ('WX_COS_KEY_KEY', key_key),
    ) if not v]
    if missing:
        fail('缺少环境变量：' + ', '.join(missing))

    os.makedirs(os.path.dirname(cert_file), exist_ok=True)
    os.makedirs(os.path.dirname(key_file), exist_ok=True)

    log(f'从对象存储拉取证书：region={region} bucket={bucket}')

    secret_id, secret_key, token = get_tmp_cred()
    client = build_client(region, secret_id, secret_key, token)

    n1 = download(client, bucket, cert_key, cert_file)
    log(f'已下载商户证书 {cert_key} -> {cert_file} ({n1} 字节)')
    n2 = download(client, bucket, key_key, key_file)
    log(f'已下载商户私钥 {key_key} -> {key_file} ({n2} 字节)')

    os.chmod(cert_file, 0o644)
    os.chmod(key_file, 0o600)

    verify(cert_file, key_file)
    log('证书校验通过：证书/私钥可用且匹配。')


if __name__ == '__main__':
    main()
