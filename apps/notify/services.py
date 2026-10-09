"""订阅消息服务：额度记录 / 任务入队 / 到点发送 / 失败重试。

统一入口：所有事件都只落成一条 NotifyTask，差别在 due_at（即时=now，定时=算好的时刻）。
发送由 apps/catalog/scheduler.py 注册的两个内置定时任务驱动：
  - dispatch_due()：每轮扫 due_at<=now 且 status=PENDING 的任务领取发送；
  - retry_failed()：扫发送失败(RETRY)且退避到期的任务重试，超过上限置终态。

原则：
  1. 通知是旁路——enqueue / send 全程 try/except，任何失败都不回滚、不冒泡到主交易；
  2. 幂等——(event, order) 唯一约束 + 领取时 PENDING->PROCESSING 条件更新，防并发/重推重复发；
  3. 额度是硬闸——扣减用 UPDATE ... WHERE count>0 条件更新，扣不到即「发不出」(NO_QUOTA 终态)；
  4. 定时提醒到点先复检订单状态，已付/已取/已开场则跳过(SKIPPED)，不打扰。
"""
import json
import logging
from datetime import timedelta

from django.conf import settings as dj_settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.notify import wechat
from apps.notify.models import NotifyTask, SubscriptionQuota

logger = logging.getLogger('app')

# 事件 -> 详情页跳转（微信小程序页面路径，不带前导斜杠）
_DETAIL_PAGE = 'pages/order/detail?orderId={oid}'


def _tmpl(event):
    """事件对应的微信模板 id（发送时以配置为准，支持后补模板后生效）。"""
    return (getattr(dj_settings, 'WX_SUBSCRIBE_TMPL', {}) or {}).get(event, '')


def _retry_max():
    return int(getattr(dj_settings, 'NOTIFY_RETRY_MAX', 3) or 0)


# ===================== 额度 =====================

def record_quota(user_id, template_ids):
    """前端授权成功（模板结果为 accept）后回记额度：每个已 accept 模板 +1。"""
    recorded = 0
    for tid in template_ids or []:
        tid = (tid or '').strip()
        if not tid:
            continue
        SubscriptionQuota.objects.get_or_create(
            user_id=user_id, template_id=tid, defaults={'count': 0})
        # 条件自增，避免并发丢更新
        SubscriptionQuota.objects.filter(user_id=user_id, template_id=tid).update(count=F('count') + 1)
        recorded += 1
    return recorded


def _consume_quota(user_id, template_id):
    """扣一条额度：UPDATE ... WHERE count>0，affected==1 才算扣到（天然防并发超发）。"""
    rows = SubscriptionQuota.objects.filter(
        user_id=user_id, template_id=template_id, count__gt=0,
    ).update(count=F('count') - 1)
    return rows == 1


def _refund_quota(user_id, template_id):
    """发送失败回补额度（重试型失败时，把刚扣的一条还回去）。"""
    SubscriptionQuota.objects.filter(
        user_id=user_id, template_id=template_id,
    ).update(count=F('count') + 1)


# ===================== 入队 =====================

def enqueue(event, order, due_at=None, payload=None):
    """幂等入队一条通知任务。已存在(同 event+order)则跳过。异常吞掉，绝不影响主链路。

    due_at 缺省=立即（事件即时型）；定时提醒由调用方算好 due_at 传入。
    """
    from apps.order.models import TicketOrder
    if isinstance(order, int):
        order = TicketOrder.objects.filter(id=order).first()
    if order is None:
        return None
    try:
        obj, created = NotifyTask.objects.get_or_create(
            event=event, order_id=order.id,
            defaults={
                'user_id': order.user_id,
                'template_id': _tmpl(event),
                'due_at': due_at or timezone.now(),
                'payload': payload or {},
            },
        )
        if created:
            logger.info('通知已入队 event=%s order=%s due_at=%s', event, order.id, obj.due_at)
        return obj
    except Exception as exc:  # noqa: BLE001 通知旁路，任何异常不外抛
        logger.exception('通知入队失败 event=%s order=%s: %s', event, getattr(order, 'id', None), exc)
        return None


def enqueue_pay_remind(order):
    """催付：due_at = 下单时刻 + (付款时限 - 提前量)。仅待付款单有意义。"""
    from apps.order.models import TicketOrder
    if order.status != TicketOrder.STATUS_PAYING:
        return None
    lead = int(getattr(dj_settings, 'PAY_REMIND_BEFORE_MIN', 4) or 0)
    due = order.created_at + timedelta(seconds=dj_settings.PAY_TIMEOUT - lead * 60)
    return enqueue(NotifyTask.EVENT_PAY_REMIND, order, due_at=due)


def enqueue_pickup_remind(order):
    """催取票：due_at = 开场时刻 - 提前量。仅已排开场时间的单有意义。"""
    from apps.order.services import order_show_start_at
    start = order_show_start_at(order)
    if start is None:
        return None
    lead = int(getattr(dj_settings, 'PICKUP_REMIND_BEFORE_MIN', 60) or 0)
    due = start - timedelta(minutes=lead)
    return enqueue(NotifyTask.EVENT_PICKUP_REMIND, order, due_at=due)


def enqueue_issued(order):
    """出票成功：立即入队（due_at=now）。"""
    return enqueue(NotifyTask.EVENT_ISSUED, order)


def enqueue_refunded(order, refund=None):
    """退款成功：立即入队，附带退款金额/原因快照（后续 Refund 可能被覆盖，落快照更稳）。"""
    payload = {}
    if refund is not None:
        payload = {
            'amount_fen': getattr(refund, 'refund_amount', None),
            'reason': getattr(refund, 'reason', '') or '',
            'type': getattr(refund, 'type', None),
        }
    return enqueue(NotifyTask.EVENT_REFUNDED, order, payload=payload)


# ===================== 发送数据构建 =====================

def _seat_names(order):
    try:
        seats = json.loads(order.seats_json) if isinstance(order.seats_json, str) else (order.seats_json or [])
    except Exception:  # noqa: BLE001
        return ''
    names = [s.get('name') or f"{s.get('row')}排{int(s.get('col', 0)) + 1}座" for s in seats]
    return '、'.join([n for n in names if n])


def _order_ctx(order):
    """构建模板所需展示数据（发送时现查，保证取票码/退款等最新）。"""
    from apps.catalog.models import Movie, Cinema
    from apps.order.services import order_show_start_at
    m = Movie.objects.filter(id=order.movie_id).first()
    c = Cinema.objects.filter(id=order.cinema_id).first()
    start = order_show_start_at(order)
    return {
        'order_no': order.order_ext_no,
        'movie_name': (m.name if m else '') or '电影票',
        'cinema_name': (c.name if c else '') or '',
        'start': start,
        'start_text': start.strftime('%Y-%m-%d %H:%M') if start else '',
        'start_md': start.strftime('%m-%d %H:%M') if start else '',
        'seats': _seat_names(order),
        'pay_yuan': f'{(order.pay_amount or 0) / 100:.2f}',
    }


def _first_ticket_code(order_id):
    from apps.order.models import Ticket
    return Ticket.objects.filter(order_id=order_id).exclude(
        ticket_code__isnull=True).exclude(ticket_code='').values_list(
        'ticket_code', flat=True).first() or ''


def _clip(text, n=20):
    """微信 thing/date 类字段有长度上限，超长截断避免发送失败(errcode 47003 等)。"""
    text = text or ''
    return text if len(text) <= n else text[:n - 1] + '…'


def build_data(event, order, payload):
    """构建微信 subscribe/send 的 data。

    ⚠️ 下方每个 key（character_string1/thing2/...）必须与公众平台「实际申请到的模板」
    的参数名逐一对应；模板批下来后在此处按真实参数名调整即可（只改这一个函数）。
    value 取订单实时快照 + payload 快照。
    """
    ctx = _order_ctx(order)
    page = _DETAIL_PAGE.format(oid=order.id)

    if event == NotifyTask.EVENT_PAY_REMIND:
        # 待支付倒计时：影片/影院/场次/待付金额/温馨提示
        data = {
            'character_string1': {'value': ctx['order_no']},
            'thing2': {'value': _clip(ctx['movie_name'])},
            'thing3': {'value': _clip(ctx['cinema_name'])},
            'date4': {'value': ctx['start_text']},
            'amount5': {'value': ctx['pay_yuan']},
            'thing6': {'value': _clip('订单即将超时关闭，请尽快完成支付')},
        }
    elif event == NotifyTask.EVENT_PICKUP_REMIND:
        # 开场前取票：影片/影院/开场时间/座位/取票码
        data = {
            'thing1': {'value': _clip(ctx['movie_name'])},
            'thing2': {'value': _clip(ctx['cinema_name'])},
            'date3': {'value': ctx['start_text']},
            'thing4': {'value': _clip(ctx['seats'], 20)},
            'character_string5': {'value': _first_ticket_code(order.id) or ctx['order_no']},
        }
    elif event == NotifyTask.EVENT_ISSUED:
        # 出票成功：订单号/影片/影院/场次/座位/取票码
        data = {
            'character_string1': {'value': ctx['order_no']},
            'thing2': {'value': _clip(ctx['movie_name'])},
            'thing3': {'value': _clip(ctx['cinema_name'])},
            'date4': {'value': ctx['start_text']},
            'thing5': {'value': _clip(ctx['seats'], 20)},
            'character_string6': {'value': _first_ticket_code(order.id) or ''},
        }
    elif event == NotifyTask.EVENT_REFUNDED:
        # 退款成功：订单号/退款金额/原因/到账时间/方式
        amount_yuan = f'{(payload.get("amount_fen") or order.pay_amount or 0) / 100:.2f}'
        data = {
            'character_string1': {'value': ctx['order_no']},
            'amount2': {'value': amount_yuan},
            'thing3': {'value': _clip(payload.get('reason') or '订单退款')},
            'date4': {'value': timezone.localtime().strftime('%Y-%m-%d %H:%M') if timezone.is_aware(timezone.now()) else timezone.now().strftime('%Y-%m-%d %H:%M')},
            'thing5': {'value': _clip('原路退回（微信支付）')},
        }
    else:
        data = {}
    return data, page


# ===================== 领取 + 发送 =====================

def _recheck(event, order, now):
    """定时提醒到点复检：返回 (ok, reason)。事件即时型无需复检。"""
    from apps.order.models import TicketOrder
    if event == NotifyTask.EVENT_PAY_REMIND:
        if order.status != TicketOrder.STATUS_PAYING:
            return False, '订单已非待付款(已付/已关闭)'
    elif event == NotifyTask.EVENT_PICKUP_REMIND:
        if order.status != TicketOrder.STATUS_WAIT_PICK:
            return False, '订单已非待取票(已取/已退/其他)'
        from apps.order.services import order_show_start_at
        start = order_show_start_at(order)
        if start and now > start:
            return False, '已过开场时间'
    return True, ''


def send_task(task):
    """发送单条任务（假定已被领取为 PROCESSING）。返回最终状态码。绝不抛异常。"""
    from apps.auths.models import AppUser
    from apps.order.models import TicketOrder

    now = timezone.now()
    order = TicketOrder.objects.filter(id=task.order_id).first()
    if order is None:
        _finalize(task, NotifyTask.STATUS_FAILED, err='订单不存在')
        return NotifyTask.STATUS_FAILED

    tmpl = _tmpl(task.event)
    if not tmpl:
        # 模板未配置：视为「发不出」常态，终态不重试（配好模板后新事件才会发）
        _finalize(task, NotifyTask.STATUS_NO_QUOTA, err='模板未配置(WX_SUBSCRIBE_TMPL)')
        return NotifyTask.STATUS_NO_QUOTA

    ok, reason = _recheck(task.event, order, now)
    if not ok:
        _finalize(task, NotifyTask.STATUS_SKIPPED, err=reason)
        return NotifyTask.STATUS_SKIPPED

    openid = AppUser.objects.filter(id=task.user_id).values_list('openid', flat=True).first()
    if not openid:
        _finalize(task, NotifyTask.STATUS_FAILED, err='用户无 openid')
        return NotifyTask.STATUS_FAILED

    # 扣额度：扣不到即「用户未授权」，NO_QUOTA 终态
    if not _consume_quota(task.user_id, tmpl):
        _finalize(task, NotifyTask.STATUS_NO_QUOTA, err='用户无订阅额度')
        return NotifyTask.STATUS_NO_QUOTA

    try:
        data, page = build_data(task.event, order, task.payload or {})
        result = wechat.send_subscribe(openid, tmpl, page, data)
    except Exception as exc:  # noqa: BLE001 构建/发送异常按重试型处理
        _refund_quota(task.user_id, tmpl)  # 回补已扣额度
        _mark_retry(task, f'{type(exc).__name__}: {exc}')
        return NotifyTask.STATUS_RETRY

    errcode = result.get('errcode')
    if errcode == 0:
        _finalize(task, NotifyTask.STATUS_SUCCESS)
        logger.info('订阅消息发送成功 event=%s order=%s', task.event, task.order_id)
        return NotifyTask.STATUS_SUCCESS

    if errcode == 43101:
        # 用户拒收/额度失效：NO_QUOTA 终态，额度不回补（本就无效）
        _finalize(task, NotifyTask.STATUS_NO_QUOTA, err=f'43101 {result.get("errmsg", "")}')
        return NotifyTask.STATUS_NO_QUOTA
    if errcode == 40003:
        _finalize(task, NotifyTask.STATUS_FAILED, err=f'40003 openid 非本 appid: {result.get("errmsg", "")}')
        logger.error('订阅消息 openid/appid 不匹配 event=%s order=%s', task.event, task.order_id)
        return NotifyTask.STATUS_FAILED

    # 其余（网络/微信 5xx/token 重试后仍失败）：回补额度 + 进重试
    _refund_quota(task.user_id, tmpl)
    _mark_retry(task, f'errcode={errcode} {result.get("errmsg", "")[:120]}')
    return NotifyTask.STATUS_RETRY


def _claim(task_id, from_status=NotifyTask.STATUS_PENDING):
    """条件更新领取：from_status -> PROCESSING，affected==1 才算抢到（防并发/重推）。"""
    return NotifyTask.objects.filter(id=task_id, status=from_status).update(
        status=NotifyTask.STATUS_PROCESSING) == 1


def _finalize(task, status, err=''):
    task.status = status
    task.err_msg = (err or '')[:255]
    if status == NotifyTask.STATUS_SUCCESS:
        task.sent_at = timezone.now()
    task.save(update_fields=['status', 'err_msg', 'sent_at', 'updated_at'])


def _mark_retry(task, err=''):
    if task.retry_count >= _retry_max():
        _finalize(task, NotifyTask.STATUS_FAILED, err=f'超过最大重试: {err}')
        return
    task.status = NotifyTask.STATUS_RETRY
    task.err_msg = (err or '')[:255]
    task.save(update_fields=['status', 'err_msg', 'updated_at'])


def _reset_stale_processing():
    """回收崩溃残留的 PROCESSING 任务（占位超 10 分钟仍未来得及落终态 -> 退回待发）。"""
    deadline = timezone.now() - timedelta(minutes=10)
    NotifyTask.objects.filter(status=NotifyTask.STATUS_PROCESSING, updated_at__lt=deadline).update(
        status=NotifyTask.STATUS_PENDING)


def dispatch_due(limit=50):
    """定时任务入口：扫到点的待发任务，领取并发送。返回各状态计数。"""
    if not dj_settings.WECHAT.get('APPID'):
        # 未配凭证时仍能安全跑：模板未配 -> NO_QUOTA；模板已配但无凭证 -> 发送失败进重试后终态。
        # 这里不 return，避免积压待发任务；仅提示配置不完整。
        logger.warning('WX_APPID 未配置，订阅消息将无法真正发出（检查 cloudrun 环境变量）')
    _reset_stale_processing()
    now = timezone.now()
    ids = list(
        NotifyTask.objects.filter(status=NotifyTask.STATUS_PENDING, due_at__lte=now)
        .order_by('due_at').values_list('id', flat=True)[:limit]
    )
    stat = {'picked': 0, 'success': 0, 'skipped': 0, 'no_quota': 0, 'retry': 0, 'failed': 0}
    _map = {
        NotifyTask.STATUS_SUCCESS: 'success', NotifyTask.STATUS_SKIPPED: 'skipped',
        NotifyTask.STATUS_NO_QUOTA: 'no_quota', NotifyTask.STATUS_RETRY: 'retry',
        NotifyTask.STATUS_FAILED: 'failed',
    }
    for tid in ids:
        if not _claim(tid):
            continue  # 已被其他进程/轮次领取
        task = NotifyTask.objects.get(id=tid)
        stat['picked'] += 1
        final = send_task(task)
        key = _map.get(final)
        if key:
            stat[key] += 1
    if stat['picked']:
        logger.info('订阅消息派发：%s', stat)
    return stat


def retry_failed(limit=50):
    """定时任务入口：退避到期的失败任务重试（指数退避按 retry_count）。"""
    _reset_stale_processing()
    now = timezone.now()
    backoff = int(getattr(dj_settings, 'NOTIFY_RETRY_BACKOFF_SECONDS', 300) or 300)
    candidates = NotifyTask.objects.filter(status=NotifyTask.STATUS_RETRY).order_by('updated_at')[:limit]
    retried = 0
    for task in candidates:
        if (now - task.updated_at).total_seconds() < backoff * max(task.retry_count, 1):
            continue  # 未到退避时间
        if not _claim(task.id, from_status=NotifyTask.STATUS_RETRY):
            continue
        NotifyTask.objects.filter(id=task.id).update(retry_count=F('retry_count') + 1)
        task.refresh_from_db()
        send_task(task)
        retried += 1
    if retried:
        logger.info('订阅消息重试：%s 条', retried)
    return {'retried': retried}
