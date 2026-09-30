"""锁座核心服务。

双层防护：
1. Redis 分布式锁（快路径，Lua 原子校验）
2. MySQL 唯一键 (schedule_id, seat_no) 兜底（Redis 抖动时插入冲突即失败）

最终座位是否可得以麻花「放单」结果收敛（麻花无锁座接口）。
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django_redis import get_redis_connection

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_lock_token
from apps.seat.models import SeatLock, SeatLockItem

logger = logging.getLogger('app')


def _redis_key(schedule_id, seat_no):
    return f'seat:lock:{schedule_id}:{seat_no}'


def _release_expired(schedule_id):
    """释放该场次已过期的锁（DB 层面兜底扫描）。"""
    now = timezone.now()
    expired = SeatLock.objects.filter(
        schedule_id=schedule_id, status=SeatLock.STATUS_HOLD, expire_at__lt=now,
    )
    for lock in expired:
        release_lock(lock.lock_token, expired=True)


def lock_seats(user_id, schedule_id, seats):
    """锁定座位。

    Args:
        user_id: 用户ID
        schedule_id: 排片ID
        seats: [{'row':'7','col':'8','name':'7排8座','price':4500}, ...]

    Returns:
        (lock_token, ttl_seconds, seat_names)
    """
    if not seats:
        raise BizError('请选择座位', code=ErrorCode.PARAM_ERROR)
    if len(seats) > 6:
        raise BizError('单次最多选 6 座', code=ErrorCode.PARAM_ERROR)

    # 0. 可售硬拦截：已过停售线的场次不允许锁座（与排片列表/建单同一口径）
    from apps.catalog.models import Schedule
    from apps.catalog import services as catalog_services
    catalog_services.ensure_sellable(
        Schedule.objects.filter(id=schedule_id, deleted=0).first()
    )

    seat_nos = [s['name'] for s in seats]

    # 1. 清理过期锁
    _release_expired(schedule_id)

    # 2. 尝试 Redis 快路径占用（不可用则降级到纯 DB 唯一键）
    ttl = getattr(settings, 'SEAT_LOCK_TTL', 600)
    redis_conn = None
    redis_occupied = []
    try:
        redis_conn = get_redis_connection('default')
        pipe = redis_conn.pipeline(transaction=True)
        for seat_no in seat_nos:
            pipe.setnx(_redis_key(schedule_id, seat_no), str(user_id))
            pipe.expire(_redis_key(schedule_id, seat_no), ttl)
        results = pipe.execute()
        # results: [setnx_result, expire_result, setnx_result, expire_result, ...]
        setnx_results = results[0::2]
        if not all(setnx_results):
            # 有座位已被占用 -> 回滚已占用的
            for i, ok_flag in enumerate(setnx_results):
                if ok_flag:
                    redis_conn.delete(_redis_key(schedule_id, seat_nos[i]))
            raise BizError('座位已被抢先，请重新选择', code=ErrorCode.SEAT_TAKEN)
        redis_occupied = seat_nos[:]
    except Exception as exc:  # noqa: BLE001
        # Redis 不可用 -> 降级到 DB 唯一键兜底（核心防超卖仍在）
        logger.warning('redis unavailable, fallback to db lock: %s', exc)
        redis_conn = None

    # 3. DB 唯一键兜底（最终防超卖）
    lock_token = gen_lock_token()
    expire_at = timezone.now() + timedelta(seconds=ttl)
    try:
        with transaction.atomic():
            lock = SeatLock.objects.create(
                lock_token=lock_token,
                schedule_id=schedule_id,
                user_id=user_id,
                seats_json=json.dumps(seats, ensure_ascii=False),
                seat_count=len(seats),
                status=SeatLock.STATUS_HOLD,
                expire_at=expire_at,
            )
            SeatLockItem.objects.bulk_create([
                SeatLockItem(lock_token=lock_token, schedule_id=schedule_id, seat_no=n)
                for n in seat_nos
            ])
    except IntegrityError:
        # DB 唯一键冲突 -> 释放 redis 占用
        if redis_conn and redis_occupied:
            for seat_no in redis_occupied:
                redis_conn.delete(_redis_key(schedule_id, seat_no))
        raise BizError('座位已被抢先，请重新选择', code=ErrorCode.SEAT_TAKEN)

    return lock_token, ttl, seat_nos


def release_lock(lock_token, expired=False):
    """释放锁（下单前取消/过期）。"""
    try:
        lock = SeatLock.objects.get(lock_token=lock_token)
    except SeatLock.DoesNotExist:
        return
    if lock.status not in (SeatLock.STATUS_HOLD,):
        return

    new_status = SeatLock.STATUS_EXPIRED if expired else SeatLock.STATUS_RELEASED
    SeatLock.objects.filter(lock_token=lock_token, status=SeatLock.STATUS_HOLD).update(
        status=new_status,
    )
    # 释放 redis + DB item
    try:
        redis_conn = get_redis_connection('default')
        for item in SeatLockItem.objects.filter(lock_token=lock_token):
            redis_conn.delete(_redis_key(item.schedule_id, item.seat_no))
    except Exception:  # noqa: BLE001
        pass  # Redis 不可用时忽略，DB item 已删除即可
    SeatLockItem.objects.filter(lock_token=lock_token).delete()


def consume_lock(lock_token, order_id):
    """锁 -> 已下单（下单成功后调用）。"""
    SeatLock.objects.filter(lock_token=lock_token, status=SeatLock.STATUS_HOLD).update(
        status=SeatLock.STATUS_CONSUMED, order_id=order_id,
    )


def verify_lock(lock_token, schedule_id):
    """校验锁是否有效（下单前）。"""
    now = timezone.now()
    try:
        lock = SeatLock.objects.get(lock_token=lock_token)
    except SeatLock.DoesNotExist:
        raise BizError('锁座已失效，请重新选座', code=ErrorCode.SEAT_LOCK_EXPIRED)
    if lock.status != SeatLock.STATUS_HOLD:
        raise BizError('锁座已失效，请重新选座', code=ErrorCode.SEAT_LOCK_EXPIRED)
    if lock.expire_at < now:
        release_lock(lock_token, expired=True)
        raise BizError('锁座已超时，请重新选座', code=ErrorCode.SEAT_LOCK_EXPIRED)
    if lock.schedule_id != schedule_id:
        raise BizError('锁座与场次不匹配', code=ErrorCode.PARAM_ERROR)
    return lock
