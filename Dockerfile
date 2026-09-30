# 拼拼电影票 后端（Django + DRF）生产镜像
# 部署目标：微信云托管（CloudBase Run）/ 任意容器平台
# 启动：gunicorn（多 worker），监听 0.0.0.0:80

FROM python:3.11-slim

# 时区：上海
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tzdata \
        default-libmysqlclient-dev \
        build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

# 先装依赖（利用 Docker 层缓存，代码改动不重复装包）
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple -r requirements.txt \
    && pip install --no-cache-dir -i https://mirrors.cloud.tencent.com/pypi/simple gunicorn

# 拷贝项目代码
COPY . .

# 收集静态文件（如有 admin/static）
RUN python manage.py collectstatic --noinput || true

# 暴露端口（与微信云托管「服务设置」端口一致）
EXPOSE 80

# 启动：gunicorn，worker 数按容器核数自适应
# 说明：微信云托管容器 CPU 通常 1~2 核，用 2~4 worker 即可；
# 若容器内存 <2G，可降为 --workers=2
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:80", \
     "--workers", "3", \
     "--threads", "2", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
