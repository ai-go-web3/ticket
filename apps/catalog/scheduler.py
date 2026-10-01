"""内置定时任务调度器（APScheduler）：进程内定时，不依赖任何外部调用。

承载两类定时任务：
1. 影片同步：按 settings.SYNC_CRON_TIMES（北京时间）每天定点跑 run_movie_sync，
   内部用进程内缓存锁（LocMem）去重，建议单副本常驻以避免多副本重复执行。
2. 麻花 token 刷新：按 settings.MAHUA['TOKEN_REFRESH']（默认 30 分钟）周期性
   refresh_token()，保证 token（有效期 2h）始终新鲜；启动时先刷一次预热。

时区固定用 settings.TIME_ZONE（Asia/Shanghai），保证按北京时间触发。

⚠️ 微信云托管注意：本方案要求服务「最小副本数 ≥ 1」。若允许缩容到 0，
   实例被回收后进程不在，定时器不会触发。
"""
import logging
from datetime import datetime

from django.conf import settings

logger = logging.getLogger('app')

_scheduler = None


def _parse_times(raw):
    """'00:30,13:00' -> [(0, 30), (13, 0)]，非法项忽略。"""
    out = []
    for part in (raw or '').split(','):
        part = part.strip()
        if not part:
            continue
        hh, _, mm = part.partition(':')
        try:
            h, m = int(hh), int(mm)
            if 0 <= h <= 23 and 0 <= m <= 59:
                out.append((h, m))
            else:
                logger.warning('忽略越界的定时时间：%s', part)
        except ValueError:
            logger.warning('忽略非法定时时间：%s', part)
    return out


def _run_sync_job():
    """APScheduler 回调：执行一次影片同步并记录结果。"""
    from apps.catalog.services import run_movie_sync
    res = run_movie_sync()
    logger.info(
        '内置影片同步任务：ran=%s skipped=%s city=%s pages=%s',
        res.get('ran'), res.get('skipped'), res.get('city'), res.get('pages'),
    )
    if res.get('error'):
        logger.error('内置影片同步任务出错：%s', res['error'])


def _run_token_job():
    """APScheduler 回调：刷新麻花 token（带锁去重，多副本只一个真正登录）。"""
    from apps.upadapter.token import scheduled_refresh
    token = scheduled_refresh()
    if token:
        logger.info('内置麻花 token 刷新任务完成 token=%s...', token[:8])
    else:
        logger.error('内置麻花 token 刷新任务失败（无可用 token）')


def _token_refresh_minutes():
    """从 settings.MAHUA['TOKEN_REFRESH']（秒）取刷新间隔分钟，<=0 表示不启用。"""
    try:
        seconds = int((getattr(settings, 'MAHUA', {}) or {}).get('TOKEN_REFRESH', 0))
    except (TypeError, ValueError):
        seconds = 0
    return seconds // 60


def start():
    """启动调度器（幂等）。注册影片同步 cron 任务 + 麻花 token 刷新 interval 任务。"""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    times = _parse_times(getattr(settings, 'SYNC_CRON_TIMES', '00:30,13:00'))
    refresh_min = _token_refresh_minutes()
    mahua_configured = bool((getattr(settings, 'MAHUA', {}) or {}).get('BASE_URL'))

    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    tz = getattr(settings, 'TIME_ZONE', 'Asia/Shanghai')
    sched = BackgroundScheduler(time_zone=tz)
    added = 0

    for i, (h, m) in enumerate(times):
        sched.add_job(
            _run_sync_job, CronTrigger(hour=h, minute=m, timezone=tz),
            id=f'sync_movies_{i}', name=f'sync_movies {h:02d}:{m:02d}',
            replace_existing=True, coalesce=True, misfire_grace_time=3600,
        )
        added += 1

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

    if added == 0:
        logger.warning('无任何定时任务需要注册，调度器未启动')
        return None

    sched.start()
    _scheduler = sched
    logger.info(
        '内置定时器已启动：sync_times=%s token_refresh=%smin tz=%s',
        times, refresh_min if mahua_configured else 'off', tz,
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
