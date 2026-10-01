# 拼拼电影票 后端（Django + DRF）生产镜像
# 部署目标：微信云托管（CloudBase Run）/ 任意容器平台
# 启动：gunicorn 单 worker + 多线程（内置 APScheduler 定时器需单进程常驻），监听 0.0.0.0:80

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

# 启动：gunicorn 单 worker + 多线程。
# 说明：项目用 APScheduler 内置定时器（apps/catalog/scheduler，在 app ready() 里启动，
#       影片同步 + 麻花 token 刷新），多 worker 会各自起一个调度器导致重复执行；
#       故固定 --workers=1，用 --threads 提高并发。若未来去掉内置定时器改外部触发，
#       可恢复多 worker。
CMD ["gunicorn", "config.wsgi:application", \
     "--bind", "0.0.0.0:80", \
     "--workers", "1", \
     "--threads", "8", \
     "--timeout", "60", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
