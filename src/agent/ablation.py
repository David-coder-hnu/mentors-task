"""A3 知识消融：不透明同构编码（T16）。

目标不是降低模型能力，而是让预训练中的 IPv6 编址知识没有可用的输入形式。
所有前缀先映射成稳定 opaque token；模型只看见 token。工具输出把地址也编码成
token，动作执行前再由 runner 侧反解成真实地址。

模型、温度、工具池、预算、epoch 与 schema 都不变，只改变观察和动作中的编码。
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..adapter.types import Response
from .decide import Action, DecisionError, Stage1State
from .prompts.ablated import render_ablated


class AblationError(ValueError):
    """A3 编码或动作反解失败。"""


@dataclass(frozen=True, slots=True)
class AblatedResponse:
    status: str
    dst: str
    src: str | None
    error: str | None
    icmp_type: int | None
    icmp_code: int | None
    hop_limit: int | None
    rtt_ms: float | None


class OpaqueEncoding:
    """固定的双向 token 映射；不包含任何地址结构。"""

    def __init__(
        self,
        prefixes: Sequence[str],
        *,
        salt: str = "mentors-task-a3-v1",
    ) -> None:
        if not salt:
            raise AblationError("salt 不能为空")
        ordered = tuple(
            str(network)
            for network in sorted(
                {
                    ipaddress.ip_network(prefix, strict=False)
                    for prefix in prefixes
                },
                key=lambda network: (network.version, int(network.network_address)),
            )
        )
        if not ordered:
            raise AblationError("至少需要一个前缀")
        self._salt = salt
        self._prefix_to_token = {
            prefix: self._token("region", prefix) for prefix in ordered
        }
        self._token_to_prefix = {
            token: prefix for prefix, token in self._prefix_to_token.items()
        }
        self._address_tokens: dict[str, str] = {}
        self._token_addresses: dict[str, str] = {}

    def _token(self, kind: str, value: str) -> str:
        digest = hashlib.sha256(f"{self._salt}\0{kind}\0{value}".encode("utf-8")).hexdigest()
        return f"{kind}-{digest[:16]}"

    def encode_prefix(self, prefix: str) -> str:
        canonical = str(ipaddress.ip_network(prefix, strict=False))
        try:
            return self._prefix_to_token[canonical]
        except KeyError as exc:
            raise AblationError(f"{prefix} 不在 A3 映射中") from exc

    def decode_prefix(self, token: str) -> str:
        try:
            return self._token_to_prefix[token]
        except KeyError as exc:
            raise AblationError(f"未知 region token：{token!r}") from exc

    def encode_address(self, address: str) -> str:
        try:
            ip = ipaddress.ip_address(address.split("/")[0])
        except ValueError as exc:
            raise AblationError(f"无效地址 {address!r}") from exc
        text = str(ip)
        if text not in self._address_tokens:
            token = self._token("node", text)
            self._address_tokens[text] = token
            self._token_addresses[token] = text
        return self._address_tokens[text]

    def decode_address(self, token: str) -> str:
        try:
            return self._token_addresses[token]
        except KeyError as exc:
            raise AblationError(f"未知 node token：{token!r}") from exc

    def encode_response(self, response: Response) -> AblatedResponse:
        return AblatedResponse(
            status=response.status,
            dst=self.encode_address(response.dst),
            src=self.encode_address(response.src) if response.src is not None else None,
            error=response.error,
            icmp_type=response.icmp_type,
            icmp_code=response.icmp_code,
            hop_limit=response.hop_limit,
            rtt_ms=response.rtt_ms,
        )

    def translate_action(self, action: Action) -> Action:
        if action.kind == "stop":
            return action
        if action.prefix is None:
            raise AblationError("探测/报告动作缺少 region token")
        real_prefix = self.decode_prefix(action.prefix)
        return Action(
            kind=action.kind,
            reason=action.reason,
            prefix=real_prefix,
            count=action.count,
        )

    def translate_action_payload(self, payload: Mapping[str, Any]) -> Action:
        """把 ablated prompt 的 `region/saturated` JSON 转成正式 Action。"""

        kind = payload.get("action")
        reason = payload.get("reason")
        if not isinstance(kind, str) or not isinstance(reason, str) or not reason.strip():
            raise AblationError("action payload 必须有 action 与非空 reason")
        if kind == "stop":
            return Action(kind="stop", reason=reason)
        if kind == "construct":
            name = payload.get("name")
            blueprint = payload.get("blueprint")
            count = payload.get("count")
            if not isinstance(name, str) or not name:
                raise AblationError("construct name 必须是非空字符串")
            if blueprint != "balanced_probe":
                raise AblationError("construct blueprint 必须是 balanced_probe")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise AblationError("construct count 必须是正整数")
            return Action(
                kind="construct",
                reason=reason,
                count=count,
                name=name,
                blueprint=blueprint,
            )
        region = payload.get("region")
        if not isinstance(region, str):
            raise AblationError("action payload 缺少 region token")
        if kind == "probe":
            count = payload.get("count")
            if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
                raise AblationError("probe count 必须是正整数")
            return Action(
                kind="probe",
                reason=reason,
                prefix=self.decode_prefix(region),
                count=count,
            )
        if kind == "report":
            saturated = payload.get("saturated")
            if not isinstance(saturated, bool):
                raise AblationError("report saturated 必须是 bool")
            return Action(
                kind="report",
                reason=reason,
                prefix=self.decode_prefix(region),
                aliased=saturated,
            )
        raise AblationError(f"未知 action：{kind!r}")

    def render_state(self, state: Stage1State) -> str:
        lines = [f"regions（{len(state.prefixes)} 个）："]
        by_real = {self.decode_prefix(self.encode_prefix(p)): p for p in state.prefixes}
        for real in sorted(by_real):
            token = self.encode_prefix(real)
            line = (
                f"- {token}：probed {state.probes.get(real, 0)}，"
                f"responded {state.replies.get(real, 0)}，"
                f"failed {state.failures.get(real, 0)}"
            )
            if real in state.reported:
                line += f"，reported saturated={str(state.reported[real]).lower()}"
            lines.append(line)
        lines.append(f"hop values：{sorted(state.hop_limits)}")
        lines.append(f"nonlocal source seen：{state.indirect_seen}")
        lines.append(f"remaining budget：{state.remaining_budget}")
        lines.append(f"epoch：{state.epoch}")
        return "\n".join(lines)

    def render_prompt(
        self,
        *,
        tools: Sequence[tuple[str, str]],
        state: Stage1State,
        construction: bool = False,
        epoch_budget: int | None = None,
    ) -> str:
        state_text = self.render_state(state)
        if epoch_budget is not None:
            state_text += f"\nEach current epoch must spend: {epoch_budget}"
        return render_ablated(
            tools=tools,
            state_text=state_text,
            construction=construction,
        )


def same_experiment_fields(
    base: Mapping[str, Any],
    ablated: Mapping[str, Any],
    *,
    allowed_differences: Sequence[str] = ("encoding",),
) -> bool:
    """A3 配置审计：除具名编码字段外，其余键值逐项相同。"""

    ignored = set(allowed_differences)
    return {
        key: value for key, value in base.items() if key not in ignored
    } == {
        key: value for key, value in ablated.items() if key not in ignored
    }


__all__ = [
    "AblatedResponse",
    "AblationError",
    "OpaqueEncoding",
    "same_experiment_fields",
]
