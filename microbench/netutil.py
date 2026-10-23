"""
出网请求的代理兜底（跨模块共用）。

很多国内用户的电脑上跑着本地代理客户端（VPN），它把 HTTP_PROXY /
HTTPS_PROXY 指向 127.0.0.1:<端口>。一旦那个软件没在运行，所有出网请求
都会抛 ProxyError —— 工作台看起来「坏了」，但界面上没有任何显式线索
（错误信息里只有一句 "Unable to connect to proxy"）。

处理方式：第一次撞到 ProxyError 就记下「环境代理是死的」，之后所有请求
用 `proxies={"http": None, "https": None}` 直连。

注意用 `proxies` 置空、而不是换成 requests.Session：
requests 的 `merge_environment_settings` 用 setdefault，键已存在就不会被
环境变量覆盖 → 效果一样是直连；但调用路径不变，测试对 `requests.post`
的 monkeypatch 仍然有效（换成 Session 会让打桩失效，测试会真的发网请求）。
"""
import sys
import time

# 直连兜底的持续时间。代理客户端可能只是临时退出/重启，过一会儿又好了 ——
# 所以不能永久标记"代理已死"，否则用户把代理开回来后请求仍然直连，
# 而 arxiv 这类站点直连会 SSL 证书校验失败（平时靠代理做 TLS 拦截）。
BYPASS_TTL_SECONDS = 300

_state = {"bypassed_until": 0.0}


def proxy_bypassed() -> bool:
    """环境代理是否已判定为不可用（True = 之后一律直连）。

    限时生效：超过 BYPASS_TTL_SECONDS 自动失效，下次请求重新走代理
    （代理客户端恢复后无需重启服务）。
    """
    return time.time() < _state["bypassed_until"]


def mark_proxy_dead(err: Exception) -> None:
    """记下「环境代理暂时不可用」，只提示一次。"""
    if proxy_bypassed():
        return
    _state["bypassed_until"] = time.time() + BYPASS_TTL_SECONDS
    print(
        f"[MicroBench] 本地代理不可用 ({err})，接下来 {BYPASS_TTL_SECONDS // 60} 分钟内改直连",
        file=sys.stderr,
    )


def direct_kwargs() -> dict:
    """代理已死时附加的 kwargs（让 requests 忽略环境代理）。"""
    if proxy_bypassed():
        return {"proxies": {"http": None, "https": None}}
    return {}
