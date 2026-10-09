"""内置定时任务调度器（APScheduler）：进程内定时，不依赖任何外部调用。

承载三类定时任务：
1. 麻花 token 刷新：按 settings.MAHUA['TOKEN_REFRESH']（默认 30 分钟）周期性
   refresh_token()，保证 token（有效期 2h）始终新鲜；启动时先刷一次预热。
2. 超时未付款订单自动关闭：每分钟扫一批。
3. 待映影片全量拉取：按 settings.COMING_PULL_MINUTES（默认 720 分钟 = 12 小时）
   周期性 run_coming_pull()，comingList 全国分页、拉到拉空为止，上限 100 条。

热映影片已改为按需实时拉取（catalog.services.pull_movies，读写穿透 + 1h 缓存），
不需要定时任务；手动同步保留 sync_movies 管理命令与运维接口作兜底。

时区固定用 settings.TIME_ZONE（Asia/Shanghai），保证按北京时间触发。

⚠️ 微信云托管注意：本方案要求服务「最小副本数 ≥ 1」。若允许缩容到 0，
   实例被回收后进程不在，定时器不会触发。
"""
import logging
from datetime import datetime

from django.conf import settings
from django.db import close_old_connections

logger = logging.getLogger('app')

_scheduler = None


def _job(fn):
    """定时任务装饰器：执行前后清理失效的 DB 连接。

    APScheduler 与 gunicorn worker 同进程常驻，MySQL 长连接空闲超过服务端
    wait_timeout 后会被服务端断开，下次任务一执行就报
    pymysql InterfaceError(0, '')。close_old_connections() 会关闭失效连接，
    让 Django 在下次查询时自动重建。
    """
    def wrapper(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()
    return wrapper


@_job
def _run_token_job():
    """APScheduler 回调：刷新麻花 token（带锁去重，多副本只一个真正登录）。"""
    from apps.upadapter.token import scheduled_refresh
    token = scheduled_refresh()
    if token:
        logger.info('内置麻花 token 刷新任务完成 token=%s...', token[:8])
    else:
        logger.error('内置麻花 token 刷新任务失败（无可用 token）')


@_job
def _run_close_expired_job():
    """APScheduler 回调：关闭超时未付款订单。"""
    from apps.order.services import close_expired_orders
    closed = close_expired_orders()
    if closed:
        logger.info('超时未付款订单自动关闭：%s 单', closed)


@_job
def _run_coming_pull_job():
    """APScheduler 回调：全量拉取待映影片（分页循环到拉空，上限 100 条）。"""
    from apps.catalog.services import run_coming_pull
    res = run_coming_pull()
    logger.info(
        '内置待映拉取任务：items=%s created=%s updated=%s stopped_by=%s',
        res.get('items'), res.get('created'), res.get('updated'), res.get('stopped_by'),
    )


@_job
def _run_dispatch_compensation_job():
    """APScheduler 回调：放单补偿（待补偿单重放/查询收敛）。"""
    from apps.upadapter.client import compensate_dispatches
    res = compensate_dispatches()
    if any(res.values()):
        logger.info('放单补偿任务：%s', res)


@_job
def _run_dispatch_query_job():
    """APScheduler 回调：每 2 分钟轮询「出票中」订单，调 /put/query（我方单号）
    收敛出票状态；与回调共用收敛方法，幂等不重复退款。"""
    from apps.upadapter.client import query_dispatching_orders
    res = query_dispatching_orders()
    if res.get('queried'):
        logger.info('出票轮询任务：%s', res)


@_job
def _run_refund_retry_job():
    """APScheduler 回调：失败微信退款重试。"""
    from apps.refund.services import retry_failed_refunds
    res = retry_failed_refunds()
    if res.get('retried'):
        logger.info('退款重试任务：%s', res)


@_job
def _run_refund_reconcile_job():
    """APScheduler 回调：退款中对账（主动查微信退款结果，通知丢失的兜底收敛）。"""
    from apps.refund.services import reconcile_refunding_refunds
    res = reconcile_refunding_refunds()
    if res.get('refunding'):
        logger.info('退款对账任务：%s', res)


@_job
def _run_notify_dispatch_job():
    """APScheduler 回调：派发订阅消息——扫到点(due_at<=now)的待发任务发送
    （出票/退款即时事件 + 催付/催取票定时提醒统一在此发送，发送前复检订单状态）。"""
    from apps.notify.services import dispatch_due
    res = dispatch_due()
    if res.get('picked'):
        logger.info('订阅消息派发任务：%s', res)


@_job
def _run_notify_retry_job():
    """APScheduler 回调：订阅消息发送失败重试（指数退避，超上限置终态）。"""
    from apps.notify.services import retry_failed
    res = retry_failed()
    if res.get('retried'):
        logger.info('订阅消息重试任务：%s', res)


def _token_refresh_minutes():
    """从 settings.MAHUA['TOKEN_REFRESH']（秒）取刷新间隔分钟，<=0 表示不启用。"""
    try:
        seconds = int((getattr(settings, 'MAHUA', {}) or {}).get('TOKEN_REFRESH', 0))
    except (TypeError, ValueError):
        seconds = 0
    return seconds // 60


def _int_setting(name, default):
    """读取整型间隔配置（秒），非法值回落 default。"""
    try:
        return int(getattr(settings, name, default))
    except (TypeError, ValueError):
        return default


def start():
    """启动调度器（幂等）。注册 token 刷新 + 超时关单 + 待映拉取三个 interval 任务。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    refresh_min = _token_refresh_minutes()
    mahua_configured = bool((getattr(settings, 'MAHUA', {}) or {}).get('BASE_URL'))
    try:
        coming_min = int(getattr(settings, 'COMING_PULL_MINUTES', 0))
    except (TypeError, ValueError):
        coming_min = 0

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.interval import IntervalTrigger

    tz = getattr(settings, 'TIME_ZONE', 'Asia/Shanghai')
    sched = BackgroundScheduler(time_zone=tz)
    added = 0

    if refresh_min > 0 and mahua_configured:
        # 启动即刷一次预热，之后每 refresh_min 分钟刷新
        sched.add_job(
            _run_token_job, IntervalTrigger(minutes=refresh_min),
            id='mahua_token_refresh', name='mahua_token_refresh',
            next_run_time=datetime.now(),  # 立即首刷
            replace_existing=True, coalesce=True, misfire_grace_time=300,
        )
        added += 1
    elif refresh_min > 0 and not mahua_configured:
        logger.warning('麻花 BASE_URL 未配置，跳过 token 刷新定时器')

    # 超时未付款订单自动关闭：每分钟扫一批（详情查询另有惰性关单兜底）
    sched.add_job(
        _run_close_expired_job, IntervalTrigger(seconds=60),
        id='order_close_expired', name='order_close_expired',
        replace_existing=True, coalesce=True, misfire_grace_time=300,
    )
    added += 1

    # 待映影片全量拉取：comingList 全国分页，循环拉到拉空（依赖麻花 token，故同样要求已配置）
    if coming_min > 0 and mahua_configured:
        sched.add_job(
            _run_coming_pull_job, IntervalTrigger(minutes=coming_min),
            id='coming_pull', name='coming_pull',
            replace_existing=True, coalesce=True, misfire_grace_time=600,
        )
        added += 1
    elif coming_min > 0 and not mahua_configured:
        logger.warning('麻花 BASE_URL 未配置，跳过待映拉取定时器')

    # 放单补偿：STATUS_PENDING 重放/查询收敛（依赖麻花，要求已配置）
    dispatch_sync_seconds = _int_setting('DISPATCH_SYNC_SECONDS', 60)
    if dispatch_sync_seconds > 0 and mahua_configured:
        sched.add_job(
            _run_dispatch_compensation_job,
            IntervalTrigger(seconds=dispatch_sync_seconds),
            id='dispatch_compensation', name='dispatch_compensation',
            replace_existing=True, coalesce=True, misfire_grace_time=300,
        )
        added += 1

    # 出票轮询：每 2 分钟全量查询「出票中」订单的放单结果（DISPATCH_QUERY_SECONDS
    # 可调，<=0 关闭；依赖麻花，要求已配置）
    dispatch_query_seconds = _int_setting('DISPATCH_QUERY_SECONDS', 120)
    if dispatch_query_seconds > 0 and mahua_configured:
        sched.add_job(
            _run_dispatch_query_job,
            IntervalTrigger(seconds=dispatch_query_seconds),
            id='dispatch_query', name='dispatch_query',
            replace_existing=True, coalesce=True, misfire_grace_time=300,
        )
        added += 1

    # 失败微信退款重试：不依赖麻花，只要配置了定时间隔即启用
    refund_retry_seconds = _int_setting('REFUND_RETRY_SECONDS', 300)
    if refund_retry_seconds > 0:
        sched.add_job(
            _run_refund_retry_job, IntervalTrigger(seconds=refund_retry_seconds),
            id='refund_retry', name='refund_retry',
            replace_existing=True, coalesce=True, misfire_grace_time=600,
        )
        added += 1

    # 退款中对账：主动查微信退款结果收敛「退款中」订单（通知丢失的兜底，
    # 每 2 分钟一轮；REFUND_RECONCILE_SECONDS 可调，<=0 关闭）
    refund_reconcile_seconds = _int_setting('REFUND_RECONCILE_SECONDS', 120)
    if refund_reconcile_seconds > 0:
        sched.add_job(
            _run_refund_reconcile_job, IntervalTrigger(seconds=refund_reconcile_seconds),
            id='refund_reconcile', name='refund_reconcile',
            replace_existing=True, coalesce=True, misfire_grace_time=300,
        )
        added += 1

    # 订阅消息派发：扫到点的待发任务发送（出票/退款即时 + 催付/催取票定时提醒）。
    # NOTIFY_DISPATCH_SECONDS 可调，<=0 关闭；发送前会复检订单状态，异常态自动跳过。
    notify_dispatch_seconds = _int_setting('NOTIFY_DISPATCH_SECONDS', 60)
    if notify_dispatch_seconds > 0:
        sched.add_job(
            _run_notify_dispatch_job, IntervalTrigger(seconds=notify_dispatch_seconds),
            id='notify_dispatch', name='notify_dispatch',
            replace_existing=True, coalesce=True, misfire_grace_time=120,
        )
        added += 1

    # 订阅消息失败重试：指数退避重发（NOTIFY_RETRY_SECONDS 可调，<=0 关闭）。
    notify_retry_seconds = _int_setting('NOTIFY_RETRY_SECONDS', 300)
    if notify_retry_seconds > 0:
        sched.add_job(
            _run_notify_retry_job, IntervalTrigger(seconds=notify_retry_seconds),
            id='notify_retry', name='notify_retry',
            replace_existing=True, coalesce=True, misfire_grace_time=300,
        )
        added += 1

    if added == 0:
        logger.warning('无任何定时任务需要注册，调度器未启动')
        return None

    sched.start()
    _scheduler = sched
    logger.info(
        '内置定时器已启动：token_refresh=%smin coming_pull=%smin '
        'dispatch_compensation=%ss dispatch_query=%ss refund_retry=%ss '
        'refund_reconcile=%ss notify_dispatch=%ss notify_retry=%ss tz=%s',
        refresh_min if mahua_configured else 'off',
        coming_min if mahua_configured else 'off',
        dispatch_sync_seconds if mahua_configured else 'off',
        dispatch_query_seconds if mahua_configured else 'off',
        refund_retry_seconds,
        refund_reconcile_seconds,
        notify_dispatch_seconds,
        notify_retry_seconds,
        tz,
    )
    return sched


def shutdown():
    """停止调度器（进程退出/测试清理用）。"""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def jobs():
    """返回当前已注册任务（调试/自测用）。"""
    return _scheduler.get_jobs() if _scheduler else []
