"""后台管理相关模型。

与 C 端 AppUser 完全隔离：AdminUser 自成体系，用 Django 密码哈希，
token 走独立的 role=admin 签名（见 authentication.py），uid 绝不落进 app_user 查询路径。
"""
from django.db import models


class AdminUser(models.Model):
    """运营后台管理员（单一超管自用，也可多开）。"""
    username = models.CharField(max_length=64, unique=True, verbose_name='登录名')
    password = models.CharField(max_length=128, verbose_name='密码哈希')  # make_password 存
    nickname = models.CharField(max_length=64, null=True, blank=True, verbose_name='昵称')
    is_active = models.SmallIntegerField(default=1, verbose_name='1启用 0停用')
    last_login_at = models.DateTimeField(null=True, blank=True, verbose_name='最近登录')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'admin_user'

    def __str__(self):
        return f'{self.id}-{self.username}'


class AdminOpLog(models.Model):
    """后台操作审计（写操作留痕，规则变更/导出等）。P0 先建表，后续接口逐步接入。"""
    admin_id = models.BigIntegerField(verbose_name='管理员ID')
    action = models.CharField(max_length=64, verbose_name='动作')       # 如 rule.update / order.export
    target = models.CharField(max_length=128, null=True, blank=True, verbose_name='对象标识')
    detail = models.JSONField(null=True, blank=True, verbose_name='详情快照')
    ip = models.CharField(max_length=64, null=True, blank=True, verbose_name='来源IP')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='时间')

    class Meta:
        db_table = 'admin_op_log'
        indexes = [models.Index(fields=['admin_id', 'created_at'])]
