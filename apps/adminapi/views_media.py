"""屏3 · 首页装修：运营上传图片，直接存 MySQL（catalog.MediaAsset）。

挂载：POST /api/v1/admin/media/upload（multipart，字段名 file）
- 仅 IsAdmin 可上传（走后台鉴权，见 views_recommend 同款 _AUTH/_PERM）；
- 服务端按「魔数字节」判定真实类型（不信任客户端 Content-Type），仅放行 JPEG/PNG/WebP；
- 单图上限 4MB；
- 返回相对路径 path=/api/v1/catalog/media/<key>，前端把它回写进 banner_url/image_url。

为什么存库而非 OSS：图片少且小，存 MySQL 自包含、随镜像/备份一起走，不引入外部存储依赖。
"""
import logging

from rest_framework.decorators import api_view, authentication_classes, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser

from apps.adminapi.authentication import AdminJWTAuthentication
from apps.adminapi.models import AdminOpLog
from apps.adminapi.permissions import IsAdmin
from apps.catalog.models import MediaAsset
from apps.common.response import ok, BizError

logger = logging.getLogger('app')

_AUTH = (AdminJWTAuthentication,)
_PERM = (IsAdmin,)

MAX_BYTES = 4 * 1024 * 1024  # 4MB

# 真实类型以魔数为准：(前缀, 额外校验) -> (content_type, 标签)
_MAGIC = (
    (b'\xff\xd8\xff', None, 'image/jpeg', 'JPG'),
    (b'\x89PNG\r\n\x1a\n', None, 'image/png', 'PNG'),
    (b'RIFF', (8, b'WEBP'), 'image/webp', 'WebP'),
)


def _sniff(data: bytes):
    """返回 (content_type, 标签) 或 None（不在白名单）。"""
    for prefix, extra, ct, label in _MAGIC:
        if data.startswith(prefix):
            if extra is not None:
                off, sig = extra
                if data[off:off + len(sig)] != sig:
                    continue
            return ct, label
    return None


@api_view(['POST'])
@authentication_classes(_AUTH)
@permission_classes(_PERM)
@parser_classes([MultiPartParser, FormParser])
def media_upload(request):
    """接收一张图片，校验后落库，返回可回写的相对访问路径。"""
    file = request.FILES.get('file')
    if not file:
        raise BizError('未收到图片文件（字段名 file）', code=40001)

    data = file.read()
    size = len(data)
    if size == 0:
        raise BizError('图片为空', code=40001)
    if size > MAX_BYTES:
        raise BizError(f'图片超过 {MAX_BYTES // 1024 // 1024}MB 上限', code=40001)

    sniffed = _sniff(data)
    if not sniffed:
        raise BizError('仅支持 JPG / PNG / WebP', code=40001)
    content_type, _label = sniffed

    admin = getattr(request, 'admin', None)
    asset = MediaAsset.objects.create(
        name=(getattr(file, 'name', '') or '')[:255] or None,
        content_type=content_type,
        size=size,
        data=data,
        uploaded_by=getattr(admin, 'id', None),
    )

    # 审计（best-effort）
    try:
        AdminOpLog.objects.create(
            admin_id=getattr(admin, 'id', 0),
            action='media.upload',
            target=asset.key,
            detail={'name': asset.name, 'size': size, 'content_type': content_type},
            ip=request.META.get('REMOTE_ADDR'),
        )
    except Exception:  # noqa: BLE001 - 审计失败不影响上传结果
        logger.exception('media upload op log failed')

    return ok({
        'key': asset.key,
        'path': asset.media_path,
        'name': asset.name,
        'size': size,
        'content_type': content_type,
    }, msg='已上传')
