"""图片服务端点（公开、免鉴权）。

运营上传的小图直接存在 MySQL（catalog.MediaAsset），这里按 key 把二进制以正确 MIME
吐出去，供小程序 <image src> 与后台预览直接引用。

挂载点：/api/v1/catalog/media/<key>
- 该路径不在 JWTAuthMiddleware 的 PROTECTED_PREFIXES 内，天然公开；
- 小程序 <image> 加载不走 wx.request 域名白名单（外链海报本就直接加载），无需额外配置；
- 内容按 key 不可变 → 长缓存 + immutable，命中后浏览器/CDN 不再回源。
"""
from django.http import HttpResponse

from apps.catalog.models import MediaAsset

# 未命中时返回的 1x1 透明 GIF（避免 <image> 报错占位难看），仅兜底用。
_TRANSPARENT_GIF = bytes.fromhex(
    '474946383961010001008000000000000000000021f90401000000002c00000000'
    '010001000002024401003b'
)


def media_asset(request, key):
    """按 key 返回图片二进制。key 非法/不存在给 404（透明兜底图 + no-store）。"""
    obj = MediaAsset.objects.filter(key=key).only('data', 'content_type').first()
    if not obj:
        resp = HttpResponse(_TRANSPARENT_GIF, content_type='image/gif', status=404)
        resp['Cache-Control'] = 'no-store'
        return resp

    resp = HttpResponse(bytes(obj.data), content_type=obj.content_type or 'application/octet-stream')
    resp['Cache-Control'] = 'public, max-age=31536000, immutable'
    resp['ETag'] = f'"{key}"'
    # 允许跨源 <img> 直接展示（小程序/后台/浏览器均受益）
    resp['Access-Control-Allow-Origin'] = '*'
    return resp
