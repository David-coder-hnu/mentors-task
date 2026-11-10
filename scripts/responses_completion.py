"""OpenAI Responses API 的最小 completion 适配器。

凭据只从显式环境变量读取，或从本机 Codex 的 provider 配置读取；脚本从不打印
它们。该入口用于把模型原文送到 A1/A2 的严格 action schema，不启动编码 Agent。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.request
from pathlib import Path
from typing import Any


def _provider_config(path: Path, provider_name: str) -> tuple[str, str, str]:
    with path.open("rb") as handle:
        document = tomllib.load(handle)
    provider = document.get("model_providers", {}).get(provider_name)
    if not isinstance(provider, dict):
        raise SystemExit(f"Codex 配置里没有 model_providers.{provider_name}")
    base_url = provider.get("base_url")
    token = (
        provider.get("experimental_bearer_token")
        or provider.get("api_key")
        or os.environ.get("OPENAI_API_KEY")
    )
    model = os.environ.get("MENTORS_TASK_MODEL") or document.get("model")
    if not all(isinstance(value, str) and value for value in (base_url, token, model)):
        raise SystemExit("base_url / bearer token / model 至少一项缺失")
    return base_url, token, model


def _output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            text = content.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if not chunks:
        raise SystemExit("Responses API 没有返回文本输出")
    return "".join(chunks).strip()


def _last_json_object(text: str) -> dict[str, Any]:
    """提取响应末尾最后一个完整 JSON 对象。

    某些 OpenAI-compatible provider 会在 JSON 前附带内部推理文本。动作协议
    只接受 JSON；这里取模型的**最终结构化结果**，若没有完整对象就拒绝。
    """

    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found = value
    if found is None:
        raise SystemExit("provider 输出里没有完整 JSON 对象")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--model", default=os.environ.get("MENTORS_TASK_MODEL"))
    parser.add_argument("--provider-name", default="custom")
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex" / "config.toml")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--raw",
        action="store_true",
        help="保留 provider 全文；默认提取并规范化最后一个 JSON 对象",
    )
    args = parser.parse_args(argv)

    env_api_key = os.environ.get("OPENAI_API_KEY")
    api_key = env_api_key
    if not (args.base_url and api_key and args.model):
        base_url, config_api_key, model = _provider_config(args.config, args.provider_name)
        args.base_url = args.base_url or base_url
        api_key = env_api_key or config_api_key
        args.model = args.model or model

    prompt = sys.stdin.read()
    if not prompt.strip():
        raise SystemExit("stdin 里没有 prompt")
    body = json.dumps(
        {
            "model": args.model,
            "instructions": (
                "Return exactly one JSON object matching the requested action schema. "
                "Do not include analysis, markdown fences, or prose."
            ),
            "input": prompt,
            "temperature": 0.0,
            "top_p": 1.0,
            "max_output_tokens": 1024,
            "text": {"format": {"type": "json_object"}},
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        args.base_url.rstrip("/") + "/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=args.timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("Responses API 返回的不是 JSON 对象")
    text = _output_text(payload)
    if args.raw:
        print(text)
    else:
        print(json.dumps(_last_json_object(text), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
