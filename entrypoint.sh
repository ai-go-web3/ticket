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

# 3) 微信商户 API 证书落盘（私钥严禁入库/进镜像，通过云托管环境变量注入）。
#    容器文件系统是临时的，重启即还原；这里在每次启动时把环境变量中的 PEM
#    内容写回文件，保证退款双向 TLS 证书在任意重启/扩缩容后始终可用。
#    约定：WX_MCH_CERT_PEM / WX_MCH_KEY_PEM 存完整 PEM 文本；路径可由
#    WX_MCH_CERT_PATH / WX_MCH_KEY_PATH 指定（默认 /app/certs/ 下）。
CERT_DIR="$(dirname "${WX_MCH_CERT_PATH:-/app/certs/apiclient_cert.pem}")"
CERT_FILE="${WX_MCH_CERT_PATH:-/app/certs/apiclient_cert.pem}"
KEY_FILE="${WX_MCH_KEY_PATH:-/app/certs/apiclient_key.pem}"
mkdir -p "$CERT_DIR"

# printf '%b' 同时兼容「真实换行」与「字面 \n」两种环境变量写法（PEM 内容不含反斜杠，安全）。
if [ -n "$WX_MCH_CERT_PEM" ] && [ ! -f "$CERT_FILE" ]; then
    printf '%b\n' "$WX_MCH_CERT_PEM" > "$CERT_FILE"
    chmod 644 "$CERT_FILE"
    echo "[entrypoint] 已落盘商户证书 -> $CERT_FILE"
fi
if [ -n "$WX_MCH_KEY_PEM" ] && [ ! -f "$KEY_FILE" ]; then
    printf '%b\n' "$WX_MCH_KEY_PEM" > "$KEY_FILE"
    chmod 600 "$KEY_FILE"
    echo "[entrypoint] 已落盘商户私钥 -> $KEY_FILE"
fi
# 落盘后回写环境变量为绝对路径，确保 Django settings 读到正确位置。
export WX_MCH_CERT_PATH="$CERT_FILE"
export WX_MCH_KEY_PATH="$KEY_FILE"

exec "$@"
