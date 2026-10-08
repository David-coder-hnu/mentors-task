"""集成测试的前置 —— 需要 Linux + CAP_NET_RAW + 一个在跑的 Punching Bag 靶场。

跑法（在 `docker/` 下）：

    docker compose --profile harness run --rm harness

`run` 会自动带上 `target` 服务并连上同一条 IPv6 网桥。宿主上直接跑会 **skip**
（Windows 建不了 ICMPv6 原始套接字），这正是 T17 要的语义：单测进 hosted CI，
集成测试排除。
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import sys

import pytest

#: 靶场容器的地址（compose 里写死 `2001:db8:1::2`）。
TARGET_IPV6 = "2001:db8:1::2"

#: harness 容器的网卡名（compose 里写死 `2001:db8:1::4` 在它上面）。
HARNESS_IFACE = "eth0"

#: 冒烟配置里的两个前缀（`docker/smoke/punchingbag-smoke.json`）：
#: dead 是 rate 1.0，0bad 是 rate 0.0。
LIVE_PREFIX = "2001:db8:dead::/64"
SILENT_PREFIX = "2001:db8:0bad::/64"


def _integration_ready() -> tuple[bool, str]:
    """这个环境能不能跑集成测试。

    **不能拿「建不建得出原始套接字」当判据**——实测过：Windows 上
    `socket(AF_INET6, SOCK_RAW, IPPROTO_ICMPV6)` 是**建得出来**的（Python 返回
    一个 fd，真正的限制在发包语义上）。用它当守卫的结果是宿主上不 skip 而是跑到
    一半炸成 error——而 error 会让 hosted CI 红，那正是 T17 要防的形状。

    所以判据是「这个环境是不是那套装置」：Linux + 有 `ip` + 有 CAP_NET_RAW。
    """
    if not sys.platform.startswith("linux"):
        return False, f"集成测试要 Linux（AF_PACKET/原始套接字/iproute2），当前 {sys.platform}"
    if shutil.which("ip") is None:
        return False, "找不到 `ip` 命令——要在 harness 容器里跑，见 docker/compose.yml"
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, socket.IPPROTO_ICMPV6)
    except OSError:
        return False, "建不了 ICMPv6 原始套接字（缺 CAP_NET_RAW）"
    sock.close()
    return True, ""


_ready, _why = _integration_ready()

requires_raw_icmp6 = pytest.mark.skipif(not _ready, reason=_why or "环境就绪")


def _route(dst_prefix: str) -> None:
    """把探测前缀指到靶场容器。

    **没有这条路由，探测包根本发不出去**：prober 会对 `2001:db8:dead::1` 直接
    做邻居发现，而那个地址不属于任何容器，ND 永远问不到 MAC。症状会伪装成
    「靶场不回包」——与 T1 的 compose 里 `prober` 服务加的那两条路由同源。
    """
    result = subprocess.run(
        ["ip", "-6", "route", "add", dst_prefix, "via", TARGET_IPV6],
        capture_output=True,
        text=True,
    )
    # 已存在不是错误（重复跑测试的正常情况）。
    if result.returncode != 0 and "File exists" not in result.stderr:
        pytest.fail(f"加路由 {dst_prefix} 失败: {result.stderr.strip()}")


@pytest.fixture(scope="session")
def routes() -> None:
    if shutil.which("ip") is None:
        pytest.fail("找不到 `ip` 命令；harness 镜像里应装上 iproute2")
    _route(LIVE_PREFIX)
    _route(SILENT_PREFIX)


@pytest.fixture
def iface(routes: None):
    from src.adapter.iface import RawIcmp6Iface

    with RawIcmp6Iface() as it:
        yield it
