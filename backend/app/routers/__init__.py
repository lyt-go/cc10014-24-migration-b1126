"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import finance, resource

# main.py 通过 module.router 取路由对象；
# finance 的固定前缀 /api/finance 必须在通用 /api/{module} 之前注册，
# 否则会被动态路径抢先匹配。
ROUTERS = [finance, resource]
