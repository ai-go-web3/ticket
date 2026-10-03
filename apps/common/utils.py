"""通用工具：单号生成 / 金额 / 幂等。"""
import time
import uuid


def gen_order_ext_no(prefix='PP'):
    """生成我方幂等订单号：PP + yyyyMMddHHmmss + 随机6位。"""
    ts = time.strftime('%Y%m%d%H%M%S')
    return f'{prefix}{ts}{uuid.uuid4().hex[:6].upper()}'


def gen_refund_no():
    return gen_order_ext_no(prefix='RF')


def gen_withdraw_no():
    return gen_order_ext_no(prefix='WD')



def gen_invite_code():
    """生成推广码（4位大写字母数字，去易混字符）。"""
    import random
    import string
    alphabet = '23456789ABCDEFGHJKLMNPQRSTUVWXYZ'
    return ''.join(random.choices(alphabet, k=6))


def yuan_to_fen(yuan):
    """元 -> 分（整数）。"""
    return int(round(float(yuan) * 100))


def fen_to_yuan(fen):
    """分 -> 元（两位小数）。"""
    return round(fen / 100, 2)
