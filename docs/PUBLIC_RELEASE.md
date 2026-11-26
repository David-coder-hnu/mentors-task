# 公开仓库发布边界

日期：2026-11-26

`mentors-task` 从这一天起以公开研究仓库的形式发布。

## 公开历史中已移除的材料

为避免再分发第三方版权内容或私人研究材料，以下文件已从当前树和全部 Git 历史中移除：

- `docs/restricted/6Seeks_A_Global_IPv6_Network_Periphery_Scanning_System.pdf`
- `docs/restricted/6Seeks_中文全文.md`
- `docs/reports/IPv6_Agent_Probing_Research_Report.md`
- `docs/reports/IPv6_Agent_Probing_Research_Report.pdf`

这些材料不再随仓库分发。需要阅读时，应通过论文 DOI、出版方页面或项目所有者管理的
私有离线备份访问。

## 后续贡献规则

- 不提交第三方论文 PDF、全文翻译或大段逐字摘录；
- 不提交导师、合作者或用户的私人研究材料；
- 引用外部工作时，使用标题、作者、DOI、官方链接和必要的短引用；
- 第三方代码通过链接与固定 commit 引用，不直接 vendor 到公开仓库；
- `docs/restricted/` 和上述导师报告文件已加入 `.gitignore`，避免再次误提交。

## 许可证提示

公开可见不等于自动授予再使用许可。仓库自有代码目前尚未附加 `LICENSE`；在使用或分发
自有代码前，应先由项目所有者确定许可证。Punching Bag 等外部组件仍遵守各自上游许可证。
