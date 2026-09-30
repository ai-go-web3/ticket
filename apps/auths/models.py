"""用户模型。"""
from django.db import models


class AppUser(models.Model):
    """用户（微信小程序）。"""
    openid = models.CharField(max_length=64, unique=True, verbose_name='微信 openid')
    unionid = models.CharField(max_length=64, null=True, verbose_name='unionid')
    phone = models.CharField(max_length=20, null=True, verbose_name='绑定手机号(加密)')
    phone_mask = models.CharField(max_length=20, null=True, verbose_name='脱敏手机号')
    nickname = models.CharField(max_length=64, null=True, verbose_name='昵称')
    avatar_url = models.CharField(max_length=255, null=True, verbose_name='头像')
    city_code = models.CharField(max_length=16, null=True, verbose_name='当前城市')
    inviter_id = models.BigIntegerField(null=True, verbose_name='邀请人/上级(分销归因)')
    status = models.SmallIntegerField(default=1, verbose_name='1正常 2冻结')
    last_login_at = models.DateTimeField(null=True, verbose_name='最后登录')
    version = models.IntegerField(default=0, verbose_name='乐观锁')
    deleted = models.SmallIntegerField(default=0, verbose_name='软删')
    created_at = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    updated_at = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        db_table = 'app_user'
        indexes = [
            models.Index(fields=['unionid']),
            models.Index(fields=['inviter_id']),
        ]

    def __str__(self):
        return f'{self.id}-{self.nickname or self.openid}'
