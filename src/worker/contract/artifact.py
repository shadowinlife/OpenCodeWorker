"""
任务产物（Artifact）契约 Schema。

产物是任务执行结束后可供下载的文件资源，存储在 Worker 宿主机的
data/artifacts/<task_id>/ 目录下，并在 artifacts 表中记录元数据。

产物生命周期：
    1. Orchestrator 在 collecting_artifacts 阶段写出文件并调用
       storage.repo.insert_artifact() 注册元数据
    2. 同时发出 artifact_ready 事件（SSE 推送）
    3. 调用方通过 GET /tasks/:id/artifacts 列出所有产物
    4. 通过 GET /tasks/:id/artifacts/:artifact_id 下载具体产物
    5. artifact_retention_days 天后自动清理文件和 DB 记录
"""
from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ArtifactType(str, Enum):
    """产物类型，决定 UI 的展示方式和自动化处理逻辑。

    workspace_snapshot:
        任务结束时工作目录的完整 .tar.gz 压缩包。
        这是最重要的产物，包含 LLM 修改后的所有文件。
        即使任务失败也会尽力生成（方便排查问题）。

    diff:
        工作区相对于初始状态的 unified diff 文件（.patch 格式）。
        仅在 workspace 为 git 方式初始化时生成（通过 git diff 产出）。

    plan:
        plan_first 模式下 LLM 生成的执行计划，纯文本或 Markdown 格式。
        即使计划被 reject/revise，也会保留每次生成的版本（版本后缀区分）。

    log:
        opencode 进程的完整运行日志（stdout + stderr 合并流）。
        排查 opencode 启动失败、模型调用异常时使用。

    stdout / stderr:
        任务中 bash 工具调用产生的标准输出 / 标准错误，单独存储。
        LLM 运行测试或构建命令时的输出会归入这两类。

    transcript:
        opencode 会话的完整对话记录（JSON 格式），包含所有
        LLM 消息和工具调用细节，用于审计和复盘。

    report:
        由特定 MCP 服务器或 Broker 生成的结构化报告（如代码扫描结果）。

    custom:
        保留给任务自定义的产物类型，由 LLM 直接写出到 artifacts/ 子目录。
    """
    workspace_snapshot = "workspace_snapshot"
    diff = "diff"
    plan = "plan"
    log = "log"
    stdout = "stdout"
    stderr = "stderr"
    transcript = "transcript"
    report = "report"
    custom = "custom"


class Artifact(BaseModel):
    """单个产物的元数据记录，对应 artifacts 表的一行。

    文件实际内容存储在宿主机文件系统，此 Schema 只记录元数据；
    下载时 API 层通过 storage.repo.get_artifact_path() 获取本地路径后
    以 FileResponse 流式返回给调用方，避免将大文件加载到内存。

    Attributes:
        artifact_id:  产物 UUID（由 Orchestrator 生成）
        task_id:      所属任务 UUID
        type:         产物类型，见 ArtifactType
        filename:     建议的下载文件名（含扩展名），用于 Content-Disposition 头
        size:         文件字节数（写出完成后填入，生成中可能为 None）
        created_at:   产物注册时间（Unix 时间戳，秒）
        expires_at:   过期时间（Unix 时间戳，秒），超过后清理任务会删除文件
        download_url: 仅在 API 响应中动态填充，DB 中存 None
    """
    artifact_id: str
    task_id: str
    type: ArtifactType
    filename: str
    size: Optional[int] = None
    created_at: float
    expires_at: Optional[float] = None
    # 动态生成，不持久化到 DB（由 API 层在响应序列化时填入）
    download_url: Optional[str] = None
