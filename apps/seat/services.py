"""锁座核心服务（纯数据库实现，不依赖 Redis）。

防超卖靠 MySQL 唯一键 seat_lock_item.(schedule_id, seat_no)：同一座位并发锁时，
第二条插入触发 IntegrityError 即失败。锁的存活/过期由 seat_lock.expire_at + 兜底扫描维护。

最终座位是否可得以麻花「放单」结果收敛（麻花无锁座接口）。
"""
import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.common.response import BizError, ErrorCode
from apps.common.utils import gen_lock_token
from apps.seat.models import SeatLock, SeatLockItem

logger = logging.getLogger('app')


def _release_expired(schedule_id):
    """释放该场次已过期的锁（DB 层面兜底扫描，删除 item 以释放唯一键）。"""
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

    # 1. 清理过期锁（释放被超时占用的唯一键）
    _release_expired(schedule_id)

    # 2. DB 唯一键原子占用（防超卖）
    ttl = getattr(settings, 'SEAT_LOCK_TTL', 600)
    lock_token = gen_lock_token()
    expire_at = timezone.now() + timedelta(seconds=ttl)
    try:
        with transaction.atomic():
            SeatLock.objects.create(
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
        raise BizError('座位已被抢先，请重新选择', code=ErrorCode.SEAT_TAKEN)

    return lock_token, ttl, seat_nos


def release_lock(lock_token, expired=False):
    """释放锁（下单前取消/过期）。删除 item 即释放座位唯一键，可被重新锁定。"""
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
    SeatLockItem.objects.filter(lock_token=lock_token).delete()


def consume_lock(lock_token, order_id):
    """锁 -> 已下单（下单成功后调用）。item 保留，座位随订单锁定，不再释放唯一键。"""
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
