# Docs Index

VibeTradingOpenCodeWorker 文档树指南。

## 主力文档（live，需要持续维护）

| 文档 | 用途 |
|---|---|
| [roadmap/opencode-worker.md](roadmap/opencode-worker.md) | 实施路线图 + Sprint backlog（**主入口**） |
| [usage-guide.md](usage-guide.md) | 部署 / 调试 / 端到端使用手册 |
| [adr/](adr/) | 已 Accepted 的架构决策记录（ADR-001~006） |

## 设计文档（design，X1 期间活跃）

| 文档 | 用途 |
|---|---|
| [design/strategy-artifact-and-scheduling.md](design/strategy-artifact-and-scheduling.md) | 主设计：strategy SKILL bundle + 调度（v2.10，5 题 + 16 OQ 全闭环） |
| [design/follow-up-recommendations.md](design/follow-up-recommendations.md) | 上述 design 的执行 roadmap（3 lane + 风险） |
| [design/meta-skill-strategy-skill-author.md](design/meta-skill-strategy-skill-author.md) | meta-skill `strategy-skill-author` 完整内容 |
| [design/worker-client-sdk-interface-design.md](design/worker-client-sdk-interface-design.md) | 上游 Worker Client SDK 接口规范 |

## 归档（archive，历史快照，不再变更）

| 文档 | 用途 |
|---|---|
| [archive/code-review-2026-05-14.md](archive/code-review-2026-05-14.md) | 2026-05-14 全量 code review（8 P0 + 12 P1 + 8 P2）；含每项的修订/闭环证据 |

## 维护约定

- **live 文档** 只列**当前仍 open** 的工作；已闭环条目折叠后链接到 archive。
- **archive 文档** 是 immutable snapshot —— 后续闭环以"修订"块**追加**而非修改原文。
- 新增 review / spike report → 命名 `XXX-YYYY-MM-DD.md` 落到 archive；live 文档只引用编号。
- ADR 一旦 Accepted 即不修改文件；后续状态变化通过新 ADR 或 review 修订块追踪。
