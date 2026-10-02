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

logger = logging.getLogger('app')

_scheduler = None


def _run_token_job():
    """APScheduler 回调：刷新麻花 token（带锁去重，多副本只一个真正登录）。"""
    from apps.upadapter.token import scheduled_refresh
    token = scheduled_refresh()
    if token:
        logger.info('内置麻花 token 刷新任务完成 token=%s...', token[:8])
    else:
        logger.error('内置麻花 token 刷新任务失败（无可用 token）')


def _run_close_expired_job():
    """APScheduler 回调：关闭超时未付款订单。"""
    from apps.order.services import close_expired_orders
    closed = close_expired_orders()
    if closed:
        logger.info('超时未付款订单自动关闭：%s 单', closed)


def _run_coming_pull_job():
    """APScheduler 回调：全量拉取待映影片（分页循环到拉空，上限 100 条）。"""
    from apps.catalog.services import run_coming_pull
    res = run_coming_pull()
    logger.info(
        '内置待映拉取任务：items=%s created=%s updated=%s stopped_by=%s',
        res.get('items'), res.get('created'), res.get('updated'), res.get('stopped_by'),
    )


def _token_refresh_minutes():
    """从 settings.MAHUA['TOKEN_REFRESH']（秒）取刷新间隔分钟，<=0 表示不启用。"""
    try:
        seconds = int((getattr(settings, 'MAHUA', {}) or {}).get('TOKEN_REFRESH', 0))
    except (TypeError, ValueError):
        seconds = 0
    return seconds // 60


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

    if added == 0:
        logger.warning('无任何定时任务需要注册，调度器未启动')
        return None

    sched.start()
    _scheduler = sched
    logger.info(
        '内置定时器已启动：token_refresh=%smin coming_pull=%smin tz=%s',
        refresh_min if mahua_configured else 'off',
        coming_min if mahua_configured else 'off',
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
