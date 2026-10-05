#!/bin/sh
# 容器启动入口：处理云托管运行时环境后再 exec 主进程（CMD）。
set -e

# 1) 把平台注入的自签证书合入系统信任库（幂等，无证书时无害跳过）。
#    微信云托管「开放接口服务」会在容器启动时往证书目录注入 api.weixin.qq.com
#    的自签证书（/app/cert/certificate.crt），并依赖容器内有 update-ca-certificates
#    完成信任（官方要求，python:3.11-slim 自带 ca-certificates 满足）。
if command -v update-ca-certificates >/dev/null 2>&1; then
    update-ca-certificates >/dev/null 2>&1 || true
fi

# 2) Python requests 默认使用 certifi 自带的 CA bundle（不含平台自签证书），
#    会导致容器内 https 调 api.weixin.qq.com 报：
#    [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate
#    这里切换为系统信任库（同时包含正规 CA 与平台自签 CA），官方推荐做法。
if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
    export REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
    export SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
    echo "[entrypoint] REQUESTS_CA_BUNDLE -> /etc/ssl/certs/ca-certificates.crt"
fi

# 3) 微信商户 API 证书从对象存储（COS）拉取（私钥严禁入库/进镜像）。
#    容器文件系统是临时的，重启/扩缩容即还原；这里在每次启动时通过
#    开放接口服务获取临时密钥，从对象存储下载证书到本地并做 openssl 自检。
#    证书已上传到云托管「对象存储」，对象键与地域/桶名由环境变量指定：
#    WX_COS_REGION / WX_COS_BUCKET / WX_COS_CERT_KEY / WX_COS_KEY_KEY
#    落地路径沿用 WX_MCH_CERT_PATH / WX_MCH_KEY_PATH（默认 /app/certs/）。
#    拉取或校验失败会 exit 1（fail-fast），避免静默降级把问题留到线上退款。
if [ -n "$WX_COS_BUCKET" ] && [ -n "$WX_COS_REGION" ]; then
    echo "[entrypoint] 从对象存储拉取微信商户 API 证书"
    python /app/scripts/fetch_certs.py
    CERT_FILE="${WX_MCH_CERT_PATH:-/app/certs/apiclient_cert.pem}"
    KEY_FILE="${WX_MCH_KEY_PATH:-/app/certs/apiclient_key.pem}"
    # 回写环境变量为绝对路径，确保 Django settings 读到正确位置。
    export WX_MCH_CERT_PATH="$CERT_FILE"
    export WX_MCH_KEY_PATH="$KEY_FILE"
else
    echo "[entrypoint] 未配置对象存储证书源（WX_COS_BUCKET/WX_COS_REGION），"
    echo "[entrypoint] 跳过证书拉取；若退款需真实打款，请补齐相关环境变量。"
fi

# 4) 数据库迁移：实例每次启动时执行（幂等，已应用的迁移自动跳过）。
#    保证「代码上线、表结构跟上」同步，避免新代码查新表（如 markup_rule）
#    而表未创建的窗口期（曾致座位/下单接口 50000）。
#    迁移失败 fail-fast：宁可实例起不来，也不带残缺 schema 对外服务。
echo "[entrypoint] 执行数据库迁移 python manage.py migrate"
python /app/manage.py migrate --noinput

exec "$@"
