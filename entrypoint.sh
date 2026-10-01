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

exec "$@"
