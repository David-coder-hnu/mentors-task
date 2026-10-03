# mentors_task — Agent 驱动的 IPv6 主动探测调研

导师课题的研究笔记仓库（不是代码项目）。研究目标：论证用 Agent / LLM 改造 IPv6 主动探测中「探测策略选择」的可行性，让 Agent 自主选择测量工具与策略，而不只是调某个固定算法的参数。

## 文件

| 文件 | 说明 |
|---|---|
| `IPv6_Agent_Probing_Research_Report.pdf` | 自写调研报告（2026-09），含 6Gen→TNet 的相关工作演进与建议架构 |
| `6Seeks_A_Global_IPv6_Network_Periphery_Scanning_System.pdf` | IEEE ToN 2025 原文，关键基线论文 |
| `6Seeks_中文全文.md` | 6Seeks 全文中文译读（含表 I–VI、图 1–16 图说、关键数字速查） |

## 约束

- **仓库保持 private**：内含 IEEE 版权论文原文与全文翻译，转 public 会构成再分发版权内容。
- 6Seeks 论文**不公开**其发现的 1.28 亿个 IPv6 外围地址，只公开源码与活跃 /48 前缀。要做对比实验需先确认数据可得性，或自行复现。

## gstack (REQUIRED — global install)

**Before doing ANY work, verify gstack is installed:**

```bash
test -d ~/.claude/skills/gstack/bin && echo "GSTACK_OK" || echo "GSTACK_MISSING"
```

If GSTACK_MISSING: STOP. Do not proceed. Tell the user:

> gstack is required for all AI-assisted work in this repo.
> Install it:
> ```bash
> git clone --depth 1 https://github.com/garrytan/gstack.git ~/.claude/skills/gstack
> cd ~/.claude/skills/gstack && ./setup --team
> ```
> Then restart your AI coding tool.

Do not skip skills, ignore gstack errors, or work around missing gstack.

Using gstack skills: After install, skills like /qa, /ship, /review, /investigate,
and /browse are available. Use /browse for all web browsing.
Use ~/.claude/skills/gstack/... for gstack file paths (the global path).
