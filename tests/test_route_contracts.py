"""路由契约护栏：装饰器挂错函数 / 端点签名被改坏时立刻变红。

背景（本轮真踩到的坑）：
抽出公共翻译循环 `_translate_chunks` 时，新函数被插到了
`@app.post("/api/paper/translate_section")` 装饰器和
`api_translate_section` **之间** → 装饰器挂到了 `_translate_chunks` 身上：

    /api/paper/translate_section -> _translate_chunks

后果：FastAPI 按 `chunks: list[str]` 解析请求体 → 用户点「翻译章节」
直接 422；而真正的 `api_translate_section` **完全没了路由**。
单元测试全绿（没人测路由挂载），只有真人点按钮才会发现。

所以这里钉死两条：
  1. 每条 `/api/*` 路由的端点函数名必须以 `api_` 开头
     （静态资源 `/`、`/manifest.webmanifest`、`/pwa/*` 不在 `/api/` 下，
      不受这条约束）
  2. 翻译类端点必须接收**一个 Pydantic 请求模型**作为唯一参数
     —— 参数变成裸 list[str] 时 FastAPI 会把它当 body 解析，422。
"""
from __future__ import annotations

import inspect

import pytest
from fastapi import routing
from pydantic import BaseModel

from microbench import app as app_mod


def _api_routes():
    out = []
    for r in app_mod.app.routes:
        if not isinstance(r, routing.APIRoute):
            continue
        if not r.path.startswith("/api/"):
            continue
        out.append(r)
    return out


def test_api_routes_exist():
    """防止"把 app 里的路由全删了"这种离谱回归也能绿。"""
    routes = _api_routes()
    assert len(routes) > 30, f"API 路由数量异常少：{len(routes)}"


def test_every_api_route_endpoint_is_an_api_function():
    """装饰器必须挂在真正的处理函数上，不能挂在抽出来的私有辅助函数上。"""
    offenders = []
    for r in _api_routes():
        ep = r.endpoint
        name = getattr(ep, "__name__", "")
        if not name.startswith("api_"):
            offenders.append(f"{r.path} -> {name}")
    assert not offenders, (
        "以下 /api/* 路由的端点函数名不以 api_ 开头（疑似装饰器挂错函数）：\n  "
        + "\n  ".join(offenders)
    )


def test_translate_routes_are_registered():
    """两条翻译路由必须存在，且指向各自的处理函数。"""
    mapping = {r.path: r.endpoint for r in _api_routes()}
    assert mapping.get("/api/paper/translate_section") is app_mod.api_translate_section
    assert mapping.get("/api/paper/translate_page") is app_mod.api_translate_page


@pytest.mark.parametrize(
    "path",
    ["/api/paper/translate_section", "/api/paper/translate_page"],
)
def test_translate_endpoints_take_one_pydantic_body(path):
    """端点必须只收一个 Pydantic 模型参数。

    参数退化成裸 list[str] / str 时，FastAPI 会按 body 解析，
    前端发 JSON 对象就会 422 —— 而且单元测试不会红。
    """
    mapping = {r.path: r.endpoint for r in _api_routes()}
    ep = mapping[path]
    sig = inspect.signature(ep)
    params = [
        p for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)
    ]
    assert len(params) == 1, f"{path} 的处理函数应只收一个参数，实际 {params}"
    ann = params[0].annotation
    assert isinstance(ann, type) and issubclass(ann, BaseModel), (
        f"{path} 的参数注解应为 Pydantic 模型，实际是 {ann!r}"
    )
