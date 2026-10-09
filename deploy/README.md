# 云服务器部署指南（Docker Compose）

目标：单台腾讯云服务器（MySQL 也在本机），跑后端容器，测试/生产两套环境隔离。
Dockerfile 无需任何修改，全部差异通过 `deploy/.env.*` 环境变量区分。

## 目录说明

```
ticket/
├── Dockerfile              # 不改，测试/生产共用同一镜像
├── docker-compose.yml      # 编排：app 容器 + 宿主机 MySQL
└── deploy/
    ├── .env.staging        # 测试环境变量（连 test_db，保留联调开关）
    ├── .env.production     # 生产环境变量（连 prod_db，联调开关已清空）
    ├── certs/              # 微信商户证书（自行放置，已被 .gitignore/.dockerignore 排除）
    │   ├── apiclient_cert.pem
    │   └── apiclient_key.pem
    └── README.md           # 本文档
```

## 一、服务器准备（一次性）

```bash
# 安装 Docker（腾讯云可配镜像源加速）
curl -fsSL https://get.docker.com | bash
systemctl enable --now docker
```

### 配置 2G swap（2核2G 机器必做）

Docker + MySQL + 应用同机约需 1.1–1.5G 内存，swap 兜底降低 MySQL 被 OOM kill 的概率：

```bash
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab   # 重启后自动挂载
free -h                                            # 确认 Swap: 2.0Gi
```

应用容器已在 `docker-compose.yml` 里限制 `mem_limit: 1g`：内存超限时只杀应用容器
（`restart: unless-stopped` 自动拉起），不牵连 MySQL 和系统。

> 注意：mem_limit 是测试/生产**各跑一套**时的限制。两套环境不要同时跑在这台
> 2G 机器上（两份应用进程会超预算）。

代码上服务器方式自选：git 拉取 / rsync / scp（注意 `.env.*` 和 `certs/` 不在 git 里，需单独传）。

## 二、MySQL 准备

数据库在本机，容器通过 `host.docker.internal`（host-gateway）访问宿主机 127.0.0.1:3306。
需确认两点：

1. **监听地址**：`bind-address` 需为 `0.0.0.0`（或至少包含 docker 网桥网段）。
   纯 `127.0.0.1` 时容器连不上（host-gateway 流量从 docker0 进入，源地址不是 127.0.0.1）。
2. **账号来源限制**：容器连入的源 IP 是 docker 网段（172.16.0.0/12），账号需允许该网段登录：

```sql
-- 最低权限方案（推荐）：测试、生产各一个账号，各只授自己的库
CREATE USER 'movie_test'@'172.%' IDENTIFIED BY '<强密码>';
GRANT ALL PRIVILEGES ON test_db.* TO 'movie_test'@'172.%';
CREATE USER 'movie_prod'@'172.%' IDENTIFIED BY '<强密码>';
GRANT ALL PRIVILEGES ON prod_db.* TO 'movie_prod'@'172.%';
FLUSH PRIVILEGES;
```

改完后更新对应 `.env.*` 里的 `MYSQL_USERNAME/MYSQL_PASSWORD`。

3. **腾讯云安全组**：确认 3306 端口**没有**对公网 0.0.0.0/0 放行（库已在本机，无需公网暴露）。

## 三、放置商户证书

```bash
mkdir -p ticket/deploy/certs
# 上传 apiclient_cert.pem / apiclient_key.pem 到该目录
chmod 600 ticket/deploy/certs/*.pem
```

退款需要真实打款时这两个文件必须就位；缺失时启动不报错，但退款降级为骨架标记。

## 四、构建与启动

```bash
cd ticket

# 测试环境（test_db，0.01 元联调链路）
docker compose --env-file deploy/.env.staging up -d --build

# 生产环境（prod_db）——先确认 .env.production 两处红线已留空：
#   PAY_AMOUNT_OVERRIDE_FEN=  /  MAHUA_COST_TOTAL_PRICE=
docker compose --env-file deploy/.env.production up -d --build
```

注意：测试和生产**不要同时跑在同一台机器的 8000 端口**上。要么分时，要么给其中一套改
`docker-compose.yml` 里的端口映射（如测试 8001、生产 8000），各自独立容器名。

验证：

```bash
docker compose --env-file deploy/.env.staging logs -f app
curl http://127.0.0.1:8000/api/v1/health  # 或任意已知接口
```

启动时 entrypoint 会自动执行 `migrate`（首次建表），迁移失败会启动失败（fail-fast），
看日志排错即可。

## 五、域名 / HTTPS（备案通过后）

小程序合法域名和微信支付回调都要求 **已备案的 https 域名**。备案通过后：

1. 域名解析 A 记录指向服务器公网 IP。
2. 宿主机装 Nginx 或 Caddy 做 443 反代 → `127.0.0.1:8000`。Caddy 自动签证书最省事：

```
# /etc/caddy/Caddyfile
你的域名 {
    reverse_proxy 127.0.0.1:8000
}
```

3. 防火墙/安全组放行 80、443。
4. 把 `deploy/.env.*` 里的 `<你的域名>` 全部替换为真实域名，`docker compose ... up -d`
   重新生效。
5. 微信公众平台配置小程序 request 合法域名；微信支付商户平台无需改（回调地址由
   `WX_NOTIFY_URL` 请求参数携带）。

## 六、日常更新发布

```bash
cd ticket
git pull                      # 或 rsync 同步代码
docker compose --env-file deploy/.env.production up -d --build
```

镜像层缓存使依赖安装只在 requirements.txt 变化时重跑，常规发版约十几秒。

## 七、已知注意事项

- **定时任务**：APScheduler 在进程内运行，compose 只起 1 个副本，勿多副本（会重复调度）。
- **两个联调开关**是测试/生产唯一的行为差异，除环境变量外镜像完全相同：
  - `PAY_AMOUNT_OVERRIDE_FEN`：测试=1（强制实付 0.01 元）；生产必须空。
  - `MAHUA_COST_TOTAL_PRICE`：测试=0.01（保证不出真实票）；生产设真实上限或空。
- **日志轮转**：compose 已配置 json-file 上限 50MB×5。
- **数据库备份**：mysqldump 定时备份 test_db/prod_db（服务器层面自行配置，如 cron + COS）。
- 旧的 `cloudrun-env*.json` 是云托管方案的控制台清单，已废弃，可删除。
