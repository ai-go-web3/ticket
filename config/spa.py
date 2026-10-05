"""运营后台 SPA（admin-ui 构建产物）同域静态托管。

方案 A：dist 打进 Docker 镜像（/app/ops_static），由 Django 直接服务 /ops/**，
同域免 CORS；SPA history 路由由「非资产路径统一回 index.html」兜底。
方案 B（自有云服务器）：Nginx 直接托管 dist 并反代 /api（见 admin-ui/nginx.conf.example），
此时镜像内无 ops_static 目录，本模块不会被挂载（见 config/urls.py）。

仅内部 1-2 名运营使用，流量极低；资产带内容 hash，长缓存 + immutable。
"""
import os
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404


def _ui_dir() -> Path:
    return Path(str(getattr(settings, 'OPS_UI_DIR', '') or ''))


def spa_index(request, *args):
    """SPA 入口：/ops/ 及所有非资产路径 -> index.html（history 路由 fallback）。"""
    index = _ui_dir() / 'index.html'
    if not index.is_file():
        raise Http404('运营后台前端未部署：请先构建 admin-ui 并打包进镜像（Docker 多阶段自动完成）')
    resp = FileResponse(index.open('rb'), content_type='text/html; charset=utf-8')
    resp['Cache-Control'] = 'no-cache'          # 入口文件必须每次校验，保证发版即生效
    resp['X-Robots-Tag'] = 'noindex, nofollow'  # 内部后台不做搜索引擎收录
    return resp


def spa_asset(request, path):
    """带内容 hash 的静态资产（/ops/assets/**）-> 一年长缓存。"""
    root = (_ui_dir() / 'assets').resolve()
    target = (root / path).resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise Http404()
    resp = FileResponse(target.open('rb'))
    resp['Cache-Control'] = 'public, max-age=31536000, immutable'
    return resp
