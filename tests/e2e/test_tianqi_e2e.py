"""
E2E 测试：天齐锂业基本面分析

测试流程
--------
1. 读取 API Keys（从 Qdata/.env 文件，缺失时通过 stdin 提示输入 HITL）
2. 启动 Worker server（subprocess uvicorn）
3. 提交 TaskRequest：
   - workspace.kind="local"，挂载 /Users/mgong/LegoNanoBot/Qdata
   - opencode 使用 dashscope DashScope provider，模型 deepseek-v4-pro
   - 任务：分析天齐锂业基本面，保存到 ./reports/tianqi_analysis.md
4. 监听 SSE /tasks/{id}/events
5. 自动处理 HITL tool_permission 事件（approve），其他 HITL 请求打印并等待 stdin 输入
6. 等待终态（completed/failed/timed_out）
7. 打印报告内容

运行方式
--------
  conda run -n legonanobot python tests/e2e/test_tianqi_e2e.py

依赖（需在运行环境中安装）：
  pip install requests httpx sseclient-py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

# ─── 第三方依赖 ─────────────────────────────────────────────────────────────
try:
    import httpx
except ImportError:
    print(
        "[E2E] 缺少依赖，请先安装: pip install httpx",
        file=sys.stderr,
    )
    sys.exit(1)

# ─── 常量 ────────────────────────────────────────────────────────────────────

QDATA_DIR = "/Users/mgong/LegoNanoBot/Qdata"
DOTENV_PATH = os.path.join(QDATA_DIR, ".env")

WORKER_PORT = 18080
WORKER_BEARER_TOKEN = "e2e-test-token"
WORKER_DATA_DIR = "/tmp/worker_e2e_data"
WORKER_SANDBOX_IMAGE = "worker-sandbox:dev-arm64"

BASE_URL = f"http://localhost:{WORKER_PORT}"

# dashscope provider 配置（与 opencode.json 对齐）
DASHSCOPE_PROVIDER_CONFIG = {
    "npm": "@ai-sdk/openai-compatible",
    "name": "DashScope (Alibaba Cloud)",
    "options": {
        "baseURL": "https://dashscope.aliyuncs.com/compatible-mode/v1"
    },
    "models": {
        "deepseek-v4-pro": {"name": "DeepSeek V4 Pro"}
    },
}

# SSE 事件类型常量（与 worker/contract/event.py TaskEventKind 对齐）
TERMINAL_EVENT_KINDS = {
    "task_completed",
    "task_failed",
    "task_aborted",
    "task_timed_out",
}


# ─── 工具函数 ────────────────────────────────────────────────────────────────

def load_env_vars() -> dict[str, str]:
    """从 Qdata/.env 加载 API keys，缺失时通过 stdin HITL 提示输入。"""
    env: dict[str, str] = {}

    # 解析 .env 文件
    if os.path.isfile(DOTENV_PATH):
        with open(DOTENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        print(f"[E2E] 从 {DOTENV_PATH} 加载环境变量")
    else:
        print(f"[E2E] 未找到 {DOTENV_PATH}，需要手动输入 API keys")

    # HITL：缺失时交互式输入
    required = ["DASHSCOPE_API_KEY", "TUSHARE_TOKEN"]
    for key in required:
        if key not in env or not env[key]:
            val = input(f"[E2E HITL] 请输入 {key}: ").strip()
            if not val:
                print(f"[E2E] 错误：{key} 不能为空", file=sys.stderr)
                sys.exit(1)
            env[key] = val

    return env


def start_worker(env_vars: dict[str, str]) -> subprocess.Popen:
    """在子进程中启动 uvicorn Worker 服务。"""
    os.makedirs(WORKER_DATA_DIR, exist_ok=True)

    # Colima 的 Docker socket 路径（与 /var/run/docker.sock 不同）
    colima_sock = os.path.expanduser("~/.colima/default/docker.sock")
    docker_host = (
        f"unix://{colima_sock}"
        if os.path.exists(colima_sock)
        else os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock")
    )

    worker_env = {
        **os.environ,
        "WORKER_BEARER_TOKEN": WORKER_BEARER_TOKEN,
        "WORKER_SANDBOX_IMAGE": WORKER_SANDBOX_IMAGE,
        "WORKER_DATA_DIR": WORKER_DATA_DIR,
        "WORKER_LOG_LEVEL": "INFO",
        "WORKER_PORT": str(WORKER_PORT),
        # 显式指定 Docker socket，确保 Colima 环境可用
        "DOCKER_HOST": docker_host,
        # broker 在本 E2E 环境中未运行（Colima 容器无法访问 macOS broker 进程）
        # 禁用 broker 后容器可直接访问外网（worker-sandbox-net internal=False）
        "WORKER_BROKER_ENABLED": "false",
        # API keys 让 worker 进程可访问（用于环境变量透传，若需要）
        **env_vars,
    }
    print(f"[E2E] DOCKER_HOST={docker_host}")

    cmd = [
        sys.executable, "-m", "uvicorn",
        "worker.main:app",
        "--host", "127.0.0.1",
        "--port", str(WORKER_PORT),
        "--log-level", "info",
    ]
    print(f"[E2E] 启动 Worker: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        env=worker_env,
        cwd=os.path.join(os.path.dirname(__file__), "..", ".."),  # 项目根目录
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def wait_worker_ready(timeout: float = 30.0) -> None:
    """轮询 /health 直到 worker 就绪或超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{BASE_URL}/health", timeout=2.0)
            if resp.status_code == 200:
                print("[E2E] Worker 就绪")
                return
        except (httpx.ConnectError, httpx.TimeoutException):
            pass
        time.sleep(0.5)
    raise RuntimeError(f"Worker 未在 {timeout}s 内就绪")


def submit_task(env_vars: dict[str, str]) -> str:
    """提交天齐锂业分析任务，返回 task_id。"""
    task_request = {
        "task_id": str(uuid.uuid4()),
        "mode": "direct_execute",
        "workspace": {
            "kind": "local",
            "local_path": QDATA_DIR,
        },
        "opencode_profile": {
            "model": "dashscope/deepseek-v4-pro",
            "providers": ["dashscope"],
            "provider_extra_config": {
                "dashscope": DASHSCOPE_PROVIDER_CONFIG,
            },
            "permission_template": "direct_execute_default",
            "permission_overrides": {
                "bash": "allow",
                "write": "allow",
                "edit": "allow",
                "webfetch": "allow",
            },
        },
        "env_policy": {
            "extra_env": {
                "DASHSCOPE_API_KEY": env_vars["DASHSCOPE_API_KEY"],
                "TUSHARE_TOKEN": env_vars["TUSHARE_TOKEN"],
            }
        },
        "broker_policy": {
            "allow_egress_hosts": ["dashscope.aliyuncs.com"],
        },
        "hitl_policy": {
            "decision_timeout_sec": 300,
            "on_timeout": "continue",
        },
        "resource_limits": {
            "timeout_sec": 3600,
        },
        "messages": [
            {
                "role": "user",
                "content": (
                    "分析天齐锂业（002466.SZ）的基本面，"
                    "查询 duckdb/ashare.duckdb 中的 fin_* 财务报表表和 stk_* 行情表，"
                    "形成完整的定量基本面分析报告，"
                    "保存到 ./reports/tianqi_analysis.md"
                ),
            }
        ],
    }

    resp = httpx.post(
        f"{BASE_URL}/tasks",
        json=task_request,
        headers={
            "Authorization": f"Bearer {WORKER_BEARER_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30.0,
    )
    if resp.status_code != 201:
        print(f"[E2E] 提交任务失败: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    task_id = resp.json()["task_id"]
    print(f"[E2E] 任务已提交: task_id={task_id}")
    return task_id


def auto_approve_decision(task_id: str, decision_id: str, kind: str) -> None:
    """自动 approve tool_permission 类 HITL 决策。"""
    payload = {
        "decision_id": decision_id,
        "choice": "approve",
        "idempotency_key": str(uuid.uuid4()),
    }
    try:
        resp = httpx.post(
            f"{BASE_URL}/tasks/{task_id}/decisions",
            json=payload,
            headers={"Authorization": f"Bearer {WORKER_BEARER_TOKEN}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            print(f"[E2E] 自动 approve 决策: decision_id={decision_id} kind={kind}")
        else:
            print(f"[E2E] 提交决策失败: {resp.status_code} {resp.text}", file=sys.stderr)
    except Exception as exc:
        print(f"[E2E] 提交决策异常: {exc}", file=sys.stderr)


def human_decision(task_id: str, decision_id: str, summary: str, options: list) -> None:
    """对非 tool_permission 的 HITL 请求进行人工决策（stdin 交互）。"""
    print(f"\n[E2E HITL] 需要人工决策:")
    print(f"  decision_id: {decision_id}")
    print(f"  摘要: {summary}")
    print(f"  可选: {options}")
    choice = input(f"  请输入决策 ({'/'.join(options)}): ").strip().lower()
    if choice not in options:
        print(f"[E2E] 无效选择 '{choice}'，使用 approve", file=sys.stderr)
        choice = "approve"

    feedback: Optional[str] = None
    if choice == "revise":
        feedback = input("  请输入修订意见: ").strip()

    payload = {
        "decision_id": decision_id,
        "choice": choice,
        "idempotency_key": str(uuid.uuid4()),
    }
    if feedback:
        payload["feedback"] = feedback

    try:
        resp = httpx.post(
            f"{BASE_URL}/tasks/{task_id}/decisions",
            json=payload,
            headers={"Authorization": f"Bearer {WORKER_BEARER_TOKEN}"},
            timeout=10.0,
        )
        print(f"[E2E] 决策已提交: {resp.status_code}")
    except Exception as exc:
        print(f"[E2E] 提交决策异常: {exc}", file=sys.stderr)


def parse_sse_events(lines_iter):
    """手动解析 SSE 文本流，生成 (event_type, data, event_id) 元组。

    SSE 格式：
        id: <cursor>\n
        event: <kind>\n
        data: <json>\n
        \n
    空行表示一个事件结束。
    """
    event_type = "message"
    data_parts: list[str] = []
    event_id: str = ""

    for raw_line in lines_iter:
        # httpx iter_lines 返回 str
        line = raw_line.rstrip("\n").rstrip("\r")
        if line == "":
            # 空行 = 事件结束，若有 data 则 yield
            if data_parts:
                yield event_type, "\n".join(data_parts), event_id
            event_type = "message"
            data_parts = []
            event_id = ""
        elif line.startswith(":"):
            # 注释行（heartbeat 等），忽略
            pass
        elif line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_parts.append(line[5:].strip())
        elif line.startswith("id:"):
            event_id = line[3:].strip()


def listen_events(task_id: str) -> str:
    """监听 SSE 事件直到终态，返回最终状态字符串。"""
    url = f"{BASE_URL}/tasks/{task_id}/events"
    headers = {
        "Authorization": f"Bearer {WORKER_BEARER_TOKEN}",
        "Accept": "text/event-stream",
        "Cache-Control": "no-cache",
    }

    print(f"[E2E] 开始监听 SSE 事件: {url}")
    final_status = "unknown"

    try:
        with httpx.stream("GET", url, headers=headers, timeout=None) as resp:
            if resp.status_code != 200:
                print(f"[E2E] SSE 连接失败: {resp.status_code}", file=sys.stderr)
                return "failed"

            for event_type, data, _eid in parse_sse_events(resp.iter_lines()):
                # 忽略 heartbeat 和纯 message 事件
                if event_type in ("heartbeat", "message", ""):
                    continue

                kind = event_type
                try:
                    payload = json.loads(data) if data else {}
                except json.JSONDecodeError:
                    payload = {}

                _print_event(kind, payload)

                # 处理 HITL 事件
                if kind == "hitl_required":
                    decision_id = payload.get("decision_id", "")
                    decision_kind = payload.get("kind", "")
                    summary = payload.get("summary", "")
                    options = payload.get("options", ["approve", "reject"])

                    # tool_permission / file_write 类自动 approve，其他走人工 stdin
                    if decision_kind in ("tool_permission", "file_write"):
                        auto_approve_decision(task_id, decision_id, decision_kind)
                    else:
                        human_decision(task_id, decision_id, summary, options)

                # 检测终态
                if kind in TERMINAL_EVENT_KINDS:
                    if kind == "task_completed":
                        final_status = "completed"
                    elif kind == "task_failed":
                        error_info = payload.get("error", {})
                        print(f"[E2E] 任务失败: {error_info}", file=sys.stderr)
                        final_status = "failed"
                    elif kind == "task_aborted":
                        final_status = "aborted"
                    break

    except Exception as exc:
        print(f"[E2E] SSE 监听异常: {exc}", file=sys.stderr)

    return final_status


def _print_event(kind: str, payload: dict) -> None:
    """格式化打印 SSE 事件。"""
    ts = time.strftime("%H:%M:%S")
    if kind == "assistant_delta":
        # 流式输出片段，只打印内容
        content = payload.get("content", "")
        print(content, end="", flush=True)
    elif kind == "tool_call_started":
        tool = payload.get("tool", "")
        args = payload.get("args", {})
        print(f"\n[{ts}] 工具调用: {tool}({json.dumps(args, ensure_ascii=False)[:120]})")
    elif kind == "tool_call_finished":
        tool = payload.get("tool", "")
        exit_code = payload.get("exit_code")
        print(f"[{ts}] 工具完成: {tool} exit={exit_code}")
    elif kind == "status_changed":
        new_status = payload.get("new_status", "")
        print(f"\n[{ts}] 状态变更: {new_status}")
    elif kind == "hitl_required":
        summary = payload.get("summary", "")
        print(f"\n[{ts}] HITL 请求: {summary}")
    else:
        # 其他事件简洁打印
        print(f"[{ts}] 事件: {kind}")


def print_report() -> None:
    """读取并打印生成的分析报告。"""
    report_path = os.path.join(QDATA_DIR, "reports", "tianqi_analysis.md")
    print("\n" + "=" * 70)
    if os.path.isfile(report_path):
        print(f"[E2E] 报告已生成: {report_path}")
        print("=" * 70)
        with open(report_path, encoding="utf-8") as f:
            print(f.read())
    else:
        print(f"[E2E] 未找到报告文件: {report_path}")
    print("=" * 70)


# ─── 主流程 ──────────────────────────────────────────────────────────────────

def main() -> None:
    print("[E2E] 天齐锂业基本面分析 E2E 测试开始")

    # 1. 加载 API Keys
    env_vars = load_env_vars()
    print(f"[E2E] DASHSCOPE_API_KEY: {env_vars['DASHSCOPE_API_KEY'][:8]}...")
    print(f"[E2E] TUSHARE_TOKEN: {env_vars['TUSHARE_TOKEN'][:8]}...")

    # 2. 启动 Worker
    worker_proc = start_worker(env_vars)
    try:
        # 等待 worker 就绪
        try:
            wait_worker_ready(timeout=60.0)
        except RuntimeError as e:
            # 打印 worker 输出帮助排查
            if worker_proc.stdout:
                out = worker_proc.stdout.read()
                print(f"[E2E] Worker 输出:\n{out}")
            raise e

        # 3. 提交任务
        task_id = submit_task(env_vars)

        # 4. 监听 SSE 事件，自动处理 HITL
        final_status = listen_events(task_id)

        print(f"\n[E2E] 任务终态: {final_status}")

        # 5. 打印报告
        if final_status == "completed":
            print_report()
        else:
            print(f"[E2E] 任务未成功完成（状态={final_status}），跳过报告输出")
            sys.exit(1)

    finally:
        # 关闭 Worker 进程
        print("[E2E] 关闭 Worker 进程...")
        worker_proc.send_signal(signal.SIGTERM)
        try:
            worker_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker_proc.kill()
        print("[E2E] Worker 已停止")


if __name__ == "__main__":
    main()
