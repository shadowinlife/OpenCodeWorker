# Code Review — 2026-05-14

> 范围：本次 review 覆盖 `src/worker/**`、`src/broker/**`、`tests/**`、`docs/**` 当前提交（commit `e32c5e5`）。
> 对照基线：README、AGENTS.md、`docs/adr/ADR-001~006`、`docs/roadmap/opencode-worker.md`、`docs/usage-guide.md`。
> 结论：Phase 1~6 框架已搭通，主链路（接收任务 → 启动容器 → 驱动 opencode → 收集产物）端到端可走通；但**多个对外承诺的安全/隔离能力与代码实际行为不一致**，建议在进入下一阶段（Phase 6 收尾 / Phase 7 准备）前先收敛。

## 摘要

| 优先级 | 数量 | 性质 |
|------|------|------|
| **P0** | 7 | 安全语义破坏、对外契约失真、终态语义错误（原 8 项，P0-2 已确认为 MVP 接受的环境降级） |
| **P1** | 12 | 可靠性 / 一致性 / 文档与实现脱节 |
| **P2** | 8 | 代码质量、维护性、deprecated API |
| **测试覆盖缺口** | 4 类 | broker/middleware/workspace/storage 单元测试缺失，integration 目录为空 |

---

## P0 — 必须收口

### P0-1 Broker CONNECT 隧道是占位实现，HTTPS 出口实际不通
- **位置**：[src/broker/proxy.py:142-163](../src/broker/proxy.py#L142-L163)、[src/broker/proxy.py:269-277](../src/broker/proxy.py#L269-L277)
- **问题**：建立 TCP 连接后立即 `writer.close()`，没有任何双向数据转发；同时 Starlette 路由表里根本没注册 CONNECT 方法，整个 `_handle_connect` 永远不会被调用。
- **影响**：`broker_enabled=True` 时容器内所有 HTTPS 出站会全部失败。
- **方向**：要么用 `asyncio.start_server` + 原始 socket 实现 CONNECT 透传（需脱离 ASGI），要么把 broker 明确移到 Phase 7，README/ADR-004/路线图同步修正语义。

### P0-2 Sandbox 网络从 `internal=True` 退回 `internal=False`（已确认为 MVP 接受的环境降级）
- **位置**：[src/worker/sandbox/manager.py:285-309](../src/worker/sandbox/manager.py#L285-L309)
- **现状**：`ensure_worker_network` 把已存在的 internal=True 网络强制删除重建为 `internal=False`，注释自陈"网络隔离由 seccomp/cap_drop 保障"。容器可直连任意外网，不经过 broker 白名单。
- **结论变更（2026-05-14）**：由于当前部署环境（Colima/本地 Docker）下 internal 网络与 broker CONNECT 隧道（见 [P0-1](#p0-1-broker-connect-隧道是占位实现https-出口实际不通)）配套链路尚未联通，强行 `internal=True` 会导致容器无法访问任何外部服务（含 LLM provider），E2E 主链路阻塞。**经讨论确认 MVP 阶段接受"容器可访问任意外网"的降级**，不再视为本轮必须收口的 P0。
- **后续要求（不阻塞 MVP）**：
  1. 在 README / ADR-004 顶部明确声明"MVP 阶段容器具备任意出网能力，不依赖 broker 白名单"，撤回"默认无外网，仅可访问 Host Broker"的对外承诺。
  2. 当 [P0-1](#p0-1-broker-connect-隧道是占位实现https-出口实际不通) / [P0-3](#p0-3-broker-进程从未被-lifespan-启动) 完成后，恢复 `internal=True` 作为默认，E2E 通过显式开关 `WORKER_SANDBOX_NETWORK_INTERNAL=false` 短路；届时本条重新升级为 P0。
  3. 在 [docs/roadmap/opencode-worker.md](../docs/roadmap/opencode-worker.md) §H1b 标注"域名级白名单实现推迟到 broker 联通之后"。

### P0-3 Broker 进程从未被 lifespan 启动
- **位置**：[src/worker/main.py:44-127](../src/worker/main.py#L44-L127)、[src/worker/orchestrator/orchestrator.py:173-175](../src/worker/orchestrator/orchestrator.py#L173-L175)
- **问题**：lifespan 只 init DB + queue worker，没有调用 `broker.proxy.run_broker` / 挂载 `create_broker_app()`；orchestrator 给容器注入 `HTTP_PROXY=http://broker:8090`，但宿主上无人监听。
- **影响**：即便用户启用 broker，容器所有 LLM/MCP 出站都会立即失败；当前 E2E 之所以能通过，是因为 `WORKER_BROKER_ENABLED=false` 把 proxy 注入也跳过了。
- **方向**：在 lifespan 中按 `broker_enabled` 启动 broker（同进程不同端口或独立进程），并加 readiness 探针。

### P0-4 `local` workspace 模式以 root + 关只读 FS 启动容器
- **位置**：[src/worker/orchestrator/orchestrator.py:136-152](../src/worker/orchestrator/orchestrator.py#L136-L152)
- **问题**：`workspace.kind == "local"` 时使用 `container_user="0:0"` + `read_only=False`。任何上游只要传 `local`，就能以 root 写挂载目录（host bind mount）。
- **影响**：MVP "非 root + read-only FS" 安全策略可被任意调用方绕过。
- **方向**：增加显式开关（如 `WORKER_ALLOW_HOST_MOUNT=1`），未开启时拒绝 `kind=local`；或者把 root 的回退改为：仅当镜像未包含 `uid 1000` 用户时才回退。

### P0-5 Agent 名错误：`"plan"`/`"build"` 而非 `"Prometheus"`/`"Sisyphus"`
- **位置**：[src/worker/adapters/opencode/driver.py:56-59](../src/worker/adapters/opencode/driver.py#L56-L59)
- **问题**：与 ADR-001 / 路线图 §1.3 / ADR-006 全部冲突。注释自陈"oh-my-openagent 未加载时使用内置 agent"——但 ADR-002/006 已固化 oh-my 3.17.2 在镜像中。
- **影响**：实际走的是 opencode 内置 `plan`/`build` agent，权限模板与计划质量都对不上 oh-my 的预期；E2E 表面跑通但行为路径与 ADR 不符。
- **是否由 oh-my 自身原因导致 fallback？（基于 e32c5e5 E2E 真实结果的复盘）**：**结论：依据现有产物无法判断**，原因如下：
  1. 镜像层面 oh-my 3.17.2 cache 已正确烘焙到 `/home/sandbox/.cache/opencode/packages/oh-my-openagent@latest/node_modules/oh-my-openagent/`（[Dockerfile.arm64:50-66](../docker/worker/Dockerfile.arm64#L50-L66)、ADR-002 §4 已实测）——结构层面 oh-my **应当**被 opencode 1.14.30 自动加载，但**镜像内未跑过 `oh-my-opencode doctor` / `GET /agent` 验证**。
  2. driver 注释由「§4 Spike 实测」改写为「oh-my-openagent 未加载时使用内置 agent」发生在同一个 e32c5e5 commit 内（与 E2E 跑通同提交），**commit message 没有给出 oh-my 加载失败的任何观测证据**（无错误日志、无 doctor 输出、无 spike 截图）。
  3. [tests/e2e/test_tianqi_e2e.py](../tests/e2e/test_tianqi_e2e.py) 仅断言报告文件生成成功，**未捕获 opencode 容器 stdout/stderr**，task DB 也只存 SSE 应用层事件，因此即便 oh-my 启动失败，本轮 E2E 也不会暴露这条信息。
  4. `plan`/`build` 是 opencode **内置 agent 名**，opencode HTTP 层会直接路由到内置实现（不经 oh-my）。这意味着 **E2E 报告成功 ≠ oh-my 已加载**，也 ≠ oh-my 未加载——两种情况下 `agent="build"` 都能跑通。
- **二次验证方案（留待后续）**：
  1. 在 [docker/worker/entrypoint.sh](../docker/worker/entrypoint.sh) 启动 `opencode serve` 前/后加一次 `curl -s http://localhost:4096/agent | jq .`，把响应写进容器日志；或在镜像构建期跑 `oh-my-opencode doctor --json` 并 `RUN` 时断言。
  2. 提交一次显式带 `"agent": "Prometheus"` 的请求，观察 opencode 是否 200 + 走 plan builder 行为，还是 4xx「unknown agent」。
  3. 若验证 oh-my 实际**已加载**，则把 `AGENT_PROMETHEUS`/`AGENT_SISYPHUS` 改回 `Prometheus`/`Sisyphus` 并删除"未加载时 fallback"注释；若验证**未加载**，则修复镜像加载路径或在 ADR-001/ADR-006 中明确"MVP 阶段使用 opencode 内置 agent，oh-my 推迟"。
- **方向**：在二次验证给出结论前，本条保留为 P0 但**不阻塞 MVP**（实际不影响 E2E 通过）。

### P0-6 `task_timed_out` 事件类型缺失
- **位置**：[src/worker/contract/event.py:69-95](../src/worker/contract/event.py#L69-L95)、[src/worker/adapters/opencode/driver.py:125-129](../src/worker/adapters/opencode/driver.py#L125-L129)
- **问题**：`TERMINAL_EVENT_KINDS` 只含 `completed/failed/aborted`，没有 `task_timed_out`。driver 把 `asyncio.TimeoutError` 转成 `RuntimeError` → queue 写 `task_failed`，DB 状态会是 `failed`，与 `TaskStatus.timed_out` 不一致。
- **影响**：SSE 上游永远收不到超时终态信号，会持续等待直到连接被服务端关闭；监控/计费层面也无法区分超时与失败。
- **方向**：新增 `TaskEventKind.task_timed_out`，driver 抛专用异常类（`TaskTimedOutError`），queue 区分处理。

### P0-7 Abort 路径丢失 → 实际写入 `task_failed`
- **位置**：[src/worker/adapters/opencode/driver.py:243-246](../src/worker/adapters/opencode/driver.py#L243-L246)、[src/worker/orchestrator/queue.py:138-148](../src/worker/orchestrator/queue.py#L138-L148)
- **问题**：HITL 拒绝/超时时抛 `RuntimeError("task aborted ...")`，但 queue 一律捕获后写 `task_failed` + `TaskStatus.failed`。用户预期 abort 走 aborted 终态，实际收到 failed。
- **方向**：driver 抛 `TaskAbortedError`，queue 单独 `except` 块写 `task_aborted`；同时 `_cleanup` 里识别 abort 来源（用户 vs 系统）。

### P0-8 Artifact 下载没限制路径必须落在 `artifacts_dir`
- **位置**：[src/worker/api/routes.py:393-416](../src/worker/api/routes.py#L393-L416)
- **问题**：仅 `Path(file_path).resolve()` 检查存在性，不约束路径必须在 `settings.artifacts_dir` 下。
- **影响**：当前 driver 写入路径都受控，未触发；但任何后续 driver bug 或恶意 fixture 都可能让 caller 通过 artifact_id 下载到任意宿主文件。
- **方向**：`resolved.relative_to(settings.artifacts_dir.resolve())` 失败即 403。

---

## P1 — 可靠性 / 一致性

### P1-9 SQLite WAL 没启用（与注释/路线图承诺不符）
- **位置**：[src/worker/storage/db.py:95-112](../src/worker/storage/db.py#L95-L112)、[src/worker/main.py:24](../src/worker/main.py#L24)
- **现状**：注释、路线图均声称"WAL 模式"，但 `init_db` 只跑 DDL。
- **方向**：`PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` + `PRAGMA busy_timeout=5000`。

### P1-10 `_next_event_id` race condition 真实存在
- **位置**：[src/worker/storage/repo.py:128-141](../src/worker/storage/repo.py#L128-L141)
- **问题**：driver 的 `_consume_sse` 与 `_handle_permission` 并发写入会撞 UNIQUE → IntegrityError → queue 写 task_failed → 整个任务挂掉。
- **方向**：per-task `asyncio.Lock`；或把 `event_id` 改为 DB 端 trigger 自增（每 task 维护 sequence 表）。

### P1-11 Metrics 全部空跑
- **位置**：[src/worker/observability/metrics.py](../src/worker/observability/metrics.py)
- **问题**：`inc_task_count` 等 helper 全仓 0 个 callsite，`/metrics` 永远空。
- **方向**：在 queue（active_tasks/duration）、driver（hitl_wait/token）、sandbox（container_start_ms）关键节点接入。

### P1-12 SSE 实时阶段是 0.5s polling
- **位置**：[src/worker/api/routes.py:218-269](../src/worker/api/routes.py#L218-L269)
- **现状**：每 task × 每订阅者 0.5s 一次 DB SELECT。
- **方向**：进程内 `asyncio.Queue/Event` 广播器；当前 MVP 可接受但建议 Phase 6 内消化。

### P1-13 `HitlPolicy.on_timeout="continue"`/`"escalate"` 路径未实现
- **位置**：[src/worker/adapters/opencode/driver.py:471-486](../src/worker/adapters/opencode/driver.py#L471-L486)、[driver.py:568-576](../src/worker/adapters/opencode/driver.py#L568-L576)
- **问题**：仅识别 `"abort"`，其它字符串走 default else，导致 `_abort_event` 不触发但 `choice_val` 又不是 `approve` → plan 路径必抛 RuntimeError。
- **方向**：完整实现三分支，或在 schema 中收窄到只支持 `"abort"`，文档同步。

### P1-14 `HitlPolicy.auto_approve` 字段未实现
- **位置**：[src/worker/contract/task.py:278-286](../src/worker/contract/task.py#L278-L286)
- **问题**：driver 完全不消费，配置是否生效对用户而言是黑盒。
- **方向**：在 `_handle_permission` 起手处先匹配 auto_approve list；或先从 schema 删除直到实现。

### P1-15 opencode `respond_permission="reject"` 可能导致死循环
- **位置**：[src/worker/adapters/opencode/driver.py:457-503](../src/worker/adapters/opencode/driver.py#L457-L503)
- **问题**：reject 在 opencode 中是单次拒绝，工具会再次 ask；driver 没设 reject 计数上限。
- **方向**：N 次 reject（如 3）后自动 `_abort_event.set()`，并写一条 `mode_escalation_suggested` 事件。

### P1-16 Queue 状态流转双写
- **位置**：[src/worker/orchestrator/queue.py:124-125](../src/worker/orchestrator/queue.py#L124-L125)、[orchestrator.py:96](../src/worker/orchestrator/orchestrator.py#L96)
- **问题**：queue 写 `starting_container`，orchestrator 又写 `preparing_workspace` 再写一次 `starting_container`。SSE 客户端会看到状态来回跳。
- **方向**：queue 只负责取队 + 进入 semaphore，状态机交给 orchestrator 完整驱动。

### P1-17 崩溃恢复只清孤儿容器，残留 `queued` 任务无人调度
- **位置**：[src/worker/main.py:69-89](../src/worker/main.py#L69-L89)
- **问题**：reaper 只查容器；DB 里 status=`queued` 但还没起容器的任务在重启后既不会被入队也不会被标 failed。路线图 §H7 决策"标 failed(orphaned)"未对全部非终态状态生效。
- **方向**：startup 时一次扫描所有非终态任务，按是否有容器 label 区分：有容器 → reap；无容器 → 直接标 failed(orphaned) + 写事件。

### P1-18 `git_subpath` 处理导致 cleanup 残留
- **位置**：[src/worker/workspace/handler.py:111-120](../src/worker/workspace/handler.py#L111-L120)
- **问题**：返回 `subpath_dir`，[orchestrator._cleanup](../src/worker/orchestrator/orchestrator.py#L237-L241) 只删 subpath，task_id 顶层目录残留。
- **方向**：`_cleanup` 接 `task_id`，按 `data_dir/workspaces/{task_id}` 整体删。

### P1-19 Artifact 文件清理未实现
- **位置**：[src/worker/api/routes.py:148-152](../src/worker/api/routes.py#L148-L152)
- **现状**：`artifact_retention_days=7` 写入 DB 后从未被读取。
- **方向**：lifespan 启动一个定时协程（asyncio）每小时扫一次过期 artifacts，删除文件 + DB 记录。

### P1-20 README 与 Roadmap 状态严重不一致
- **位置**：[README.md](../README.md)
- **问题**：README §当前状态写 Phase 1 进行中、Phase 2 待开始；Roadmap 里 Phase 2/3/5/6 大部分 ✅。
- **方向**：本 review 同步修正 README（见单独提交）。

---

## P2 — 代码质量

### P2-21 过量延迟 import
- **位置**：[main.py:55-99](../src/worker/main.py#L55-L99)、[orchestrator.py:118,209](../src/worker/orchestrator/orchestrator.py#L118)、[driver.py:113,140,628](../src/worker/adapters/opencode/driver.py#L113)
- **方向**：除有明确循环依赖原因之外，全部提到模块顶部。

### P2-22 `asyncio.get_event_loop()` 在 3.12+ 已废弃
- **位置**：[sandbox/manager.py 多处](../src/worker/sandbox/manager.py)、[driver.py:744-749](../src/worker/adapters/opencode/driver.py#L744)、[workspace/handler.py:102,144](../src/worker/workspace/handler.py#L102)
- **方向**：换 `asyncio.get_running_loop()` 或直接 `asyncio.to_thread()`。

### P2-23 异常吞噬过宽
- **位置**：[main.py:84-88,119-123](../src/worker/main.py#L84)、[orchestrator._cleanup](../src/worker/orchestrator/orchestrator.py#L227)
- **方向**：`logger.exception` + 缩窄异常类型；至少不要把所有 Exception 都 `pass`。

### P2-24 `_provider_key_env_var` 映射不一致
- **位置**：[orchestrator.py:353-365](../src/worker/orchestrator/orchestrator.py#L353)
- **问题**：`dashscope`/`alibaba`/`alibaba-cn`/`alibabacloud` 多名映射 + 与 opencode 官方 provider ID 未对齐；e2e/usage-guide 内出现 `dashscope/deepseek-v4-pro` 与 `alibaba-cn/deepseek-v4-pro` 两种写法。
- **方向**：统一以 opencode 官方 provider ID 为准，集中常量表。

### P2-25 `app = create_app()` 在 module import 时触发 settings 加载
- **位置**：[main.py:163](../src/worker/main.py#L163)
- **问题**：单测 import worker.main 即要求 `WORKER_BEARER_TOKEN`。
- **方向**：保留 `app` 但用 lazy property，或在测试 conftest 提前注入 env。

### P2-26 `BrokerPolicy.mcp_servers` 是僵尸字段
- **位置**：[src/worker/contract/task.py:308-310](../src/worker/contract/task.py#L308-L310)
- **方向**：在字段注释里强标 "Phase 7 占位，当前忽略"，或直接移除。

### P2-27 `broker_client/` 是空模块
- **位置**：[src/worker/broker_client/](../src/worker/broker_client/)
- **方向**：要么实装一个轻量 client（封装 `POST /broker/tasks/:id/policy`），要么删掉占位包并更新目录文档。

### P2-28 `delete_session` / `_stop_container_sync` / `_cleanup` 静默吞错
- **位置**：[client.py:78-82](../src/worker/adapters/opencode/client.py#L78)、[sandbox/manager.py:225-240](../src/worker/sandbox/manager.py#L225)、[orchestrator.py:227-250](../src/worker/orchestrator/orchestrator.py#L227)
- **方向**：至少 `logger.warning` + 错误类型；保留宽松吞错可加注释解释为何故意吞。

---

## 测试覆盖缺口

| 类别 | 现状 | 缺口 |
|------|------|------|
| 单元 | 41 用例（event_stream / state_machine / permission_mapper） | broker policy/proxy、middleware、storage repo、workspace 解压安全、orchestrator 配置构建（env/permission JSON）、driver state machine 完全没测试 |
| 集成 | `tests/integration/` 是空目录 | `stub_opencode_server` fixture 已写好但未串到任何 integration 用例 |
| E2E | 仅 `tests/e2e/test_tianqi_e2e.py`，依赖 Colima + DASHSCOPE token | 不可在 CI 跑；Phase 6 退出检查的"集成测试"实质 pending |
| 安全回归 | shell 手测 7/7（见 Roadmap §Phase 2.5） | 未沉淀为脚本/Pytest 自动化 |

---

## 建议修复顺序

### Sprint 0（先修使其与文档承诺一致）
1. P0-5 二次验证：在容器内 `GET /agent` 或 `oh-my-opencode doctor --json`，确认 oh-my 是否真已加载——若已加载则改回 `Prometheus`/`Sisyphus`；若未加载则修镜像或在 ADR 中明确 MVP 走内置 agent
2. P0-6/P0-7 abort/timeout 路径区分终态（新增 `task_timed_out` event；driver 抛专用异常类型，queue 区分处理）
3. P0-1/P0-3 broker 进程与 CONNECT 隧道：要么完整实现 CONNECT 隧道 + 启动 broker 进程；要么把 README/ADR-004 修正为"broker 是 Phase 7"，且明确 `broker_enabled=True` 不是默认安全模型。**P0-2 已确认 MVP 接受 `internal=False` 的环境降级**，仅需同步修订 README/ADR-004 的对外安全声明（撤回"默认无外网"承诺）
4. P0-4 给 `local` 模式加显式开关 `WORKER_ALLOW_HOST_MOUNT=1`，未开则拒绝 `kind=local`
5. P0-8 artifact 下载路径强约束在 `artifacts_dir`

### Sprint 1（可靠性）
6. P1-9/P1-10：WAL + per-task lock
7. P1-11：把 metrics helper 接进 queue/driver/sandbox 关键路径
8. P1-13/P1-14：on_timeout 全路径实现 + auto_approve 实现（或从 schema 收窄）
9. P1-16/P1-17：状态流转去重 + queued 任务恢复策略
10. P1-19：磁盘 GC 定时任务

### Sprint 2（质量）
11. 顶层化延迟 import / 替换 `get_event_loop()`
12. README 与 Roadmap 状态对齐；AGENTS.md 重新定位
13. 补 broker、middleware、workspace 解压安全的单元测试；把 `stub_opencode_server` 串到 integration 测试

---

## 与 ADR / Roadmap 的对齐建议

| 项 | 建议动作 |
|---|---|
| ADR-004 broker 边界 | 在 ADR 顶部加一行"实现状态：CONNECT 隧道与进程启停未完成（见 P0-1/P0-3）；**MVP 阶段容器网络为 `internal=False`，可直连任意外网，broker 白名单暂未生效**（见 P0-2）" |
| Roadmap §Phase 2.5 安全回归 | 标注"测试用例当前未自动化，shell 手测见 commit 历史" |
| Roadmap §Phase 6 退出检查 | 把"`/metrics` 端点格式响应"细化为"格式 ✅，counter 接入 ❌（见 P1-11）" |
| README §当前状态 | Phase 2/3/5/6 改为 ✅；状态表项后补 review 编号 |
| AGENTS.md | 与 README 对齐定位为"通用 OpenCode Worker"，而非"vibe-trading 业务 worker" |
