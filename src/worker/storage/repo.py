"""
Storage 仓库层：封装 SQLite 表的有类型 CRUD 操作。

设计原则：
    - 每个函数接收 `db: aiosqlite.Connection` 参数，方便单元测试
      时注入 mock DB。
    - 所有写操作内部就 commit，调用方无需手动管理事务。
    - 返回类型全部是 Pydantic 威理，不将 aiosqlite.Row 暴露到上层。
    - 事件序号（event_id）由 _next_event_id() 互斥地分配，靠 DB UNIQUE
      约束保证同一 task 内不重复。

    函数分为四组：Tasks / Events / Decisions / Artifacts
"""
from __future__ import annotations

import json
import time
from typing import Optional

import aiosqlite

from worker.contract.artifact import Artifact
from worker.contract.decision import DecisionRequest, DecisionResponse, PendingDecision
from worker.contract.event import TaskEvent, TaskEventKind
from worker.contract.task import TaskRequest, TaskResponse, TaskStatus


# ---------------------------------------------------------------------------
# Tasks — 任务元数据的增删改查
# ---------------------------------------------------------------------------

async def insert_task(
    db: aiosqlite.Connection,
    request: TaskRequest,
    status: TaskStatus = TaskStatus.pending,
) -> TaskResponse:
    """将新任务持久化到 DB 并返回快照。

    task_id 已在 TaskRequest.task_id 中预分配（UUID v4），
    调用方可指定相同 task_id 实现幂等重提交，但 PRIMARY KEY 冲突
    时会抛出 IntegrityError（幂等性需由 API 层处理）。
    """
    now = time.time()
    task_id = request.task_id  # already set by schema default_factory
    await db.execute(
        """
        INSERT INTO tasks (id, status, mode, request_json, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (task_id, status.value, request.mode.value, request.model_dump_json(), now, now),
    )
    await db.commit()
    return TaskResponse(
        task_id=task_id,
        status=status,
        mode=request.mode,
        created_at=now,
        updated_at=now,
    )


async def get_task(db: aiosqlite.Connection, task_id: str) -> Optional[TaskResponse]:
    """按 task_id 查询任务快照，不存在返回 None。"""
    async with db.execute(
        "SELECT id, status, mode, container_id, opencode_session_id, "
        "created_at, updated_at, completed_at FROM tasks WHERE id = ?",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return TaskResponse(
        task_id=row["id"],
        status=TaskStatus(row["status"]),
        mode=row["mode"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        container_id=row["container_id"],
        opencode_session_id=row["opencode_session_id"],
    )


async def update_task_status(
    db: aiosqlite.Connection,
    task_id: str,
    status: TaskStatus,
    container_id: Optional[str] = None,
    opencode_session_id: Optional[str] = None,
) -> None:
    """更新任务状态并可同时写入 container_id / opencode_session_id。

    COALESCE 语法保证只有非 NULL 的参数才会覆盖存量字段，
    防止多次调用时误清 container_id。
    进入终态时自动填入 completed_at（只填一次）。
    """
    now = time.time()
    completed_at = now if status in {
        TaskStatus.completed, TaskStatus.failed,
        TaskStatus.aborted, TaskStatus.timed_out,
    } else None
    await db.execute(
        """
        UPDATE tasks
           SET status = ?,
               container_id = COALESCE(?, container_id),
               opencode_session_id = COALESCE(?, opencode_session_id),
               updated_at = ?,
               completed_at = COALESCE(completed_at, ?)
         WHERE id = ?
        """,
        (status.value, container_id, opencode_session_id, now, completed_at, task_id),
    )
    await db.commit()


async def delete_task(db: aiosqlite.Connection, task_id: str) -> bool:
    """删除任务及其所有关联记录（CASCADE）。返回 True 表示确实删除了一行。"""
    cur = await db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    await db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Events — append-only 事件流。不允许修改和删除
# ---------------------------------------------------------------------------

async def _next_event_id(db: aiosqlite.Connection, task_id: str) -> int:
    """获取任务的下一个序号。

    多个协程并发写入时，不同协程可能读到相同 MAX(event_id)。
    UNIQUE(task_id, event_id) 约束保证冖冲行抛出错误而非静默丢数据。
    Phase 1 为单进程单取队，暂时不会冖冲；如未来改为并发调度
    需加 SELECT ... FOR UPDATE 或换用乐观锁重试。
    """
    async with db.execute(
        "SELECT COALESCE(MAX(event_id), 0) + 1 FROM task_events WHERE task_id = ?",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    return row[0]


async def insert_event(
    db: aiosqlite.Connection,
    task_id: str,
    kind: TaskEventKind,
    payload: dict | None = None,
) -> TaskEvent:
    """写入一条事件并返回完整的 TaskEvent 对象。

    返回的 TaskEvent 可直接用于内存中的 SSE 广播，
    无需二次读取 DB。
    """
    event_id = await _next_event_id(db, task_id)
    now = time.time()
    payload_json = json.dumps(payload or {})
    await db.execute(
        """
        INSERT INTO task_events (event_id, task_id, kind, payload_json, ts)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_id, task_id, kind.value, payload_json, now),
    )
    await db.commit()
    return TaskEvent(event_id=event_id, task_id=task_id, kind=kind,
                     payload=payload or {}, ts=now, cursor=event_id)


async def get_events_after(
    db: aiosqlite.Connection,
    task_id: str,
    after_cursor: int,
) -> list[TaskEvent]:
    """获取 event_id > after_cursor 的所有事件，按 event_id 升序排列。

    SSE 断线重连时用于补发错过的历史事件。
    after_cursor=0 表示从头开始获取该任务的全量事件。
    """
    async with db.execute(
        """
        SELECT event_id, kind, payload_json, ts
          FROM task_events
         WHERE task_id = ? AND event_id > ?
         ORDER BY event_id ASC
        """,
        (task_id, after_cursor),
    ) as cur:
        rows = await cur.fetchall()
    return [
        TaskEvent(
            event_id=row["event_id"],
            task_id=task_id,
            kind=TaskEventKind(row["kind"]),
            payload=json.loads(row["payload_json"]),
            ts=row["ts"],
            cursor=row["event_id"],
        )
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Decisions — HITL 决策入库与幽等解析
# ---------------------------------------------------------------------------

async def insert_decision(
    db: aiosqlite.Connection,
    task_id: str,
    req: DecisionRequest,
) -> PendingDecision:
    """将 HITL 决策请求写入 DB（INSERT OR IGNORE 庂等）。

    若相同 decision_id 已存在（如 Orchestrator 重启后重新发送），
    操作不产生副作用，直接返回新构造的 PendingDecision。
    """
    now = time.time()
    await db.execute(
        """
        INSERT OR IGNORE INTO decisions
               (id, task_id, kind, status, request_json, idempotency_key, created_at)
        VALUES (?, ?, ?, 'pending', ?, ?, ?)
        """,
        (req.decision_id, task_id, req.kind.value,
         req.model_dump_json(), None, now),
    )
    await db.commit()
    return PendingDecision(
        decision_id=req.decision_id,
        task_id=task_id,
        kind=req.kind,
        status="pending",
        request=req,
        created_at=now,
    )


async def resolve_decision(
    db: aiosqlite.Connection,
    decision_id: str,
    resp: DecisionResponse,
) -> bool:
    """将决策标记为 resolved 并写入响应。

    仅更新 status='pending' 的决策，防止重复解析。
    返回 True 表示成功更新，False 表示决策不存在或已解析。
    """
    now = time.time()
    cur = await db.execute(
        """
        UPDATE decisions
           SET status = 'resolved', response_json = ?, resolved_at = ?,
               idempotency_key = COALESCE(idempotency_key, ?)
         WHERE id = ? AND status = 'pending'
        """,
        (resp.model_dump_json(), now, resp.idempotency_key, decision_id),
    )
    await db.commit()
    return cur.rowcount > 0


async def get_pending_decision(
    db: aiosqlite.Connection,
    task_id: str,
) -> Optional[PendingDecision]:
    async with db.execute(
        "SELECT id, kind, request_json, created_at FROM decisions "
        "WHERE task_id = ? AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
        (task_id,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    req = DecisionRequest.model_validate_json(row["request_json"])
    return PendingDecision(
        decision_id=row["id"],
        task_id=task_id,
        kind=req.kind,
        status="pending",
        request=req,
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

async def insert_artifact(
    db: aiosqlite.Connection,
    artifact: Artifact,
    file_path: Optional[str] = None,
) -> None:
    await db.execute(
        """
        INSERT INTO artifacts (id, task_id, type, filename, file_path, size, created_at, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (artifact.artifact_id, artifact.task_id, artifact.type.value,
         artifact.filename, file_path, artifact.size,
         artifact.created_at, artifact.expires_at),
    )
    await db.commit()


async def list_artifacts(
    db: aiosqlite.Connection,
    task_id: str,
) -> list[Artifact]:
    async with db.execute(
        "SELECT id, type, filename, size, created_at, expires_at "
        "FROM artifacts WHERE task_id = ? ORDER BY created_at ASC",
        (task_id,),
    ) as cur:
        rows = await cur.fetchall()
    from worker.contract.artifact import ArtifactType
    return [
        Artifact(
            artifact_id=row["id"],
            task_id=task_id,
            type=ArtifactType(row["type"]),
            filename=row["filename"],
            size=row["size"],
            created_at=row["created_at"],
            expires_at=row["expires_at"],
        )
        for row in rows
    ]


async def get_artifact_path(
    db: aiosqlite.Connection,
    artifact_id: str,
    task_id: str,
) -> Optional[str]:
    async with db.execute(
        "SELECT file_path FROM artifacts WHERE id = ? AND task_id = ?",
        (artifact_id, task_id),
    ) as cur:
        row = await cur.fetchone()
    return row["file_path"] if row else None
