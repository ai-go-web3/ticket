# 半岛电影 · 后端（Django + DRF）

> 电影购票小程序后端。技术栈：**Django 4.2 + Django REST Framework + MySQL 8 + Redis**。
> 供应商为**麻花电影（放单模式）**，方案详见 `../docs/` 目录。
> 配套前端：`../web/`（原生微信小程序）。

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env   # 填写 MySQL / Redis / 微信 / 麻花 配置

# 3. 建库 + 迁移（首次）
# 手动创建数据库 movie_ticket（或执行 docs/schema.sql）
python manage.py makemigrations
python manage.py migrate

# 4. 同步城市（国标↔麻花映射表灌库）
python manage.py sync_city

# 5. 启动
python manage.py runserver
```

> **微信登录配置**：`.env` 里填 `WX_APPID` / `WX_SECRET`（小程序 AppID/AppSecret）。
> 留空时登录走「开发降级」（`openid = dev_<code>`），可本地联调；生产必须配置。
> 前端 `web/utils/config.js` 把 `USE_MOCK` 改为 `false`、`BASE_URL` 指向本后端即可打通登录。

## 目录结构

```
ticket/
├── config/                 # 项目配置（settings/urls/wsgi/asgi）
│   ├── settings.py         # 数据库/Redis/DRF/微信/麻花/业务参数
│   └── urls.py             # 主路由，挂载各业务模块
├── apps/                   # 业务模块（按域分层）
│   ├── common/             # 公共：统一响应/异常/JWT工具/幂等/中间件
│   ├── auths/              # 登录态：wx.login、手机号绑定、JWT 鉴权
│   ├── catalog/            # 基础数据：城市/影片/影院/排期
│   ├── seat/               # 座位：本地锁座（Redis + DB 唯一键防超卖）
│   ├── order/              # 订单：状态机、放单映射、支付流水、票券
│   ├── pay/                # 支付：微信统一下单、支付回调
│   ├── refund/             # 退款：拦截/纠纷/出票失败自动退
│   ├── distributor/        # 分销：CPS 归因/佣金/钱包/提现/风控
│   └── upadapter/          # 上游适配：麻花 SPI 客户端、token 缓存、回调入口
├── manage.py
├── requirements.txt
└── .env.example
```

## 模块职责

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `common` | 统一响应 `{code,msg,data,traceId}`、`BizError`、JWT 工具、幂等、对账/任务日志表 | 全项目基础设施 |
| `auths` | wx.login→openid→签发 JWT；getPhoneNumber 绑定手机号 | token 用内部 JWT |
| `catalog` | 城市/影片/影院/排期查询 | 数据由麻花回调同步落库 |
| `seat` | 本地锁座并发控制 | **Redis 快路径 + DB 唯一键兜底**，最终以麻花放单结果收敛 |
| `order` | 订单状态机（集中管理迁移）、建单、放单映射、票券 | 状态机禁止散落改 status，带 version 乐观锁 |
| `pay` | 微信统一下单、支付回调（验签→幂等→触发出票） | 先支付后放单 |
| `refund` | 拦截（未出票）/纠纷（已出票）/出票失败自动退 | 先麻花受理后退用户 |
| `distributor` | 归因绑定、计佣、钱包 CAS、提现冻结 | 层级≤2、T+N 结算 |
| `upadapter` | 麻花 SPI 接口、token 缓存（2h）、回调入口 | 严禁每请求取 token |

## 核心设计

### 订单状态机
```
待支付(10) → 出票中(20) → 待取票(30) → 已完成(40)
   │            │            │
   └→已关闭(50) └→出票失败(60)└→退款中(70)→已退款(80)
                                   └→纠纷中(90)
```
状态迁移集中在 `apps/order/statemachine.py`，非法迁移直接拒绝。

### 锁座防超卖（双层）
1. Redis 分布式锁（快路径，Lua/setnx 原子占用）
2. MySQL 唯一键 `seat_lock_item(schedule_id, seat_no)`（兜底，Redis 抖动时插入冲突即失败）

> 麻花无锁座接口，座位最终可得以「放单」结果收敛；放单失败自动退款引导重选。

### 金额
统一以「分」存 BIGINT，避免浮点误差。前端展示时转「元」。

## API 一览（前缀 /api/v1）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/auth/wx-login` | 微信登录 |
| POST | `/auth/bind-phone` | 绑定手机号 |
| GET | `/catalog/cities` `/movies` `/cinemas` `/schedules` | 基础数据 |
| POST | `/seat/lock` `/seat/release` | 锁座/释放 |
| POST | `/order/` | 建单 |
| GET | `/order/list` `/order/{id}` | 订单列表/详情 |
| POST | `/order/{id}/cancel` `/order/{id}/refund` | 取消/退款 |
| POST | `/pay/unified` `/pay/callback` | 统一下单/支付回调 |
| GET | `/distributor/summary` `/team` `/wallet` `/commissions` | 分销 |
| POST | `/distributor/bind` `/distributor/withdraw` | 归因/提现 |
| POST | `/up/callback/order` `/up/callback/dispute` | 麻花回调 |

## 测试

```bash
# 冒烟测试（用 SQLite 跑通核心闭环：鉴权/锁座/防超卖/建单）
# 见 docs 或直接跑 django check
python manage.py check
```

## 待办（P0 收尾）

- [ ] 接 Celery 把放单/出票轮询转异步（当前同步调用）
- [ ] 微信支付回调完整验签
- [ ] 麻花字段级映射（待 apifox 正式报文）
- [ ] 排片/影片同步定时任务（麻花回调 + 定时兜底）
- [ ] 对账批任务
