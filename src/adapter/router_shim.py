"""Response 层的外围/间接响应合成器（T15 / D9）。

它不修改 Punching Bag，也不发送额外网络包。给定手工拓扑后，把探测目标映射成
ICMPv6 Destination Unreachable 形式的 `Response`：

- `dst` 是策略探测的地址；
- `src` 是未被寻址的最后一跳路由器；
- `icmp_type=1`；
- hop limit 可由规则配置；
- 同一 aliased 前缀只暴露一个合成来源，使“响应很多，真实实体没有增加”。

`Stage2Truth` 只在 runner 侧由同一个拓扑生成，因此 shim 的 `src` 集合与
`GroundTruth.routers` 可以直接做契约测试。
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from .probe_pool import ProbeRun, ProbeTarget
from .truth import Stage2Truth
from .types import ICMP6_DEST_UNREACH, Response


class RouterShimError(ValueError):
    """手工拓扑不满足 shim 契约。"""


@dataclass(frozen=True, slots=True)
class RouterRule:
    """一个前缀的合成响应规则。"""

    prefix: str
    routers: tuple[str, ...]
    aliased: bool = False
    hop_limit: int = 64
    response_rate: float = 1.0

    def __post_init__(self) -> None:
        try:
            network = ipaddress.ip_network(self.prefix, strict=False)
        except ValueError as exc:
            raise RouterShimError(f"前缀 {self.prefix!r} 解析失败：{exc}") from exc
        if not network:
            raise RouterShimError("前缀不能为空")
        if not 0.0 <= self.response_rate <= 1.0:
            raise RouterShimError(
                f"{self.prefix} 的 response_rate 必须在 [0, 1]，得到 {self.response_rate}"
            )
        if not self.routers and self.response_rate > 0.0:
            raise RouterShimError(f"{self.prefix} 会回包，但没有任何合成路由器")
        for router in self.routers:
            try:
                addr = ipaddress.ip_address(router)
            except ValueError as exc:
                raise RouterShimError(f"路由器地址 {router!r} 无效：{exc}") from exc
            if not isinstance(addr, ipaddress.IPv6Address):
                raise RouterShimError(f"路由器 {router} 必须是 IPv6 地址")
        if self.aliased and len(set(self.routers)) != 1:
            raise RouterShimError(
                f"aliased 前缀 {self.prefix} 必须只暴露一个来源，得到 {self.routers}"
            )
        if self.aliased and self.response_rate != 1.0:
            raise RouterShimError(
                f"aliased 前缀 {self.prefix} 必须对每个地址回包，"
                f"response_rate 应是 1.0，得到 {self.response_rate}"
            )
        if not 1 <= self.hop_limit <= 255:
            raise RouterShimError(f"hop_limit 必须在 [1, 255]，得到 {self.hop_limit}")


class RouterShim:
    """把目标地址映射成确定性的间接响应。"""

    def __init__(self, rules: Iterable[RouterRule], *, seed: int = 0) -> None:
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise RouterShimError(f"seed 必须是非负整数，得到 {seed!r}")
        ordered = tuple(rules)
        if not ordered:
            raise RouterShimError("至少需要一条路由规则")
        normalized: dict[ipaddress.IPv6Network, RouterRule] = {}
        for rule in ordered:
            if not isinstance(rule, RouterRule):
                raise RouterShimError(f"规则必须是 RouterRule，得到 {type(rule).__name__}")
            net = ipaddress.ip_network(rule.prefix, strict=False)
            if net in normalized:
                raise RouterShimError(f"前缀 {net} 重复")
            normalized[net] = rule
        self._rules = tuple(
            sorted(normalized.items(), key=lambda item: item[0].prefixlen, reverse=True)
        )
        self._seed = seed
        self.truth = self._build_truth()

    def _build_truth(self) -> Stage2Truth:
        routers: set[str] = set()
        prefixes: dict[str, dict[str, object]] = {}
        topology: dict[str, tuple[str, ...]] = {}
        for net, rule in self._rules:
            key = str(net)
            routers.update(rule.routers)
            prefixes[key] = {"aliased": rule.aliased, "n_routers": len(set(rule.routers))}
            topology[key] = tuple(dict.fromkeys(rule.routers))
        return Stage2Truth(
            routers=frozenset(routers),
            prefixes=prefixes,
            topology=topology,
            seed_map={},
        )

    def ground_truth(self) -> Stage2Truth:
        return self.truth

    def _rule_for(self, dst: str) -> RouterRule | None:
        addr = ipaddress.ip_address(dst)
        for net, rule in self._rules:
            if addr in net:
                return rule
        return None

    def _digest(self, dst: str) -> bytes:
        # seed=0 是既有 T15 契约的默认形态；为它保留原始摘要，避免在引入
        # seed 相关状态时静默改变旧测试与旧回放。非零 seed 加入显式命名空间。
        material = (
            dst.encode("ascii")
            if self._seed == 0
            else f"{self._seed}\0{dst}".encode("ascii")
        )
        return hashlib.sha256(material).digest()

    def _responds(self, dst: str, rule: RouterRule) -> bool:
        if rule.response_rate <= 0.0:
            return False
        if rule.response_rate >= 1.0:
            return True
        value = int.from_bytes(self._digest(dst)[:8], "big") / (1 << 64)
        return value < rule.response_rate

    def _pick_router(self, dst: str, rule: RouterRule) -> str:
        if len(rule.routers) == 1:
            return rule.routers[0]
        digest = self._digest(dst)
        index = int.from_bytes(digest[:8], "big") % len(rule.routers)
        return rule.routers[index]

    def response_for(self, dst: str) -> Response:
        """为目标生成一条确定的间接响应；范围外目标返回无回包。"""

        ipaddress.ip_address(dst)
        rule = self._rule_for(dst)
        if rule is None or not self._responds(dst, rule):
            return Response(status="sent_no_reply", dst=dst)
        router = self._pick_router(dst, rule)
        return Response(
            status="received",
            dst=dst,
            src=router,
            icmp_type=ICMP6_DEST_UNREACH,
            icmp_code=0,
            hop_limit=rule.hop_limit,
            rtt_ms=0.0,
        )

    def run(self, targets: Iterable[ProbeTarget | str]) -> ProbeRun:
        """满足 `ProbeExecutor` 协议的纯 Response 层执行器。"""

        dsts = [target.dst if isinstance(target, ProbeTarget) else str(target) for target in targets]
        responses = tuple(self.response_for(dst) for dst in dsts)
        return ProbeRun(responses=responses, sent=len(responses))


__all__ = ["RouterRule", "RouterShim", "RouterShimError"]
