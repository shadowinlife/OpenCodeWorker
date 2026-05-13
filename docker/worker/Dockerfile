# ──────────────────────────────────────────────────────────────────────────────
# Worker Sandbox Image
#
# 基础镜像：debian:bookworm-slim（x86_64/Linux）
# 包含：Bun（opencode 运行时）、opencode、oh-my-openagent 插件
#
# Pin 版本（更新时需同步更新 ADR-002 / ADR-006）：
#   - opencode:  1.14.30
#   - oh-my-openagent: 3.17.2（opencode plugin，随 opencode 包缓存加载）
#   - Bun:       latest stable（通过 install.sh 安装，opencode 依赖）
#
# 安全设计：
#   - 非 root 用户（uid 1000，gid 1000，username: sandbox）
#   - 根 FS 运行时设为 read-only（--read-only docker run 参数，非 Dockerfile 层）
#   - /tmp 为 tmpfs（--tmpfs docker run 参数）
#   - 不内置任何 API key / secret（全部通过容器 env 注入）
#   - OPENCODE_DISABLE_AUTOUPDATE=1 在 docker run 时通过 env 注入
#
# 构建命令（本机开发）：
#   docker build -t worker-sandbox:dev -f docker/worker/Dockerfile .
#
# 注意事项：
#   - 目标平台 linux/amd64（生产服务器）；本机 Apple Silicon 需加 --platform linux/amd64
#   - 本机 Colima 环境默认为 linux/aarch64，开发阶段可先构建 aarch64 版本验证流程
# ──────────────────────────────────────────────────────────────────────────────

FROM debian:bookworm-slim AS base

# ── 系统依赖 ──────────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        unzip \
        && rm -rf /var/lib/apt/lists/*

# ── 创建非 root 用户 ──────────────────────────────────────────────────────────
RUN groupadd -g 1000 sandbox && \
    useradd -u 1000 -g 1000 -m -s /bin/bash sandbox

# ── 安装 Bun（opencode 运行时）────────────────────────────────────────────────
# Bun 安装脚本会写入 /root/.bun；移动到系统路径供所有用户使用
RUN curl -fsSL https://bun.sh/install | bash && \
    mv /root/.bun /usr/local/bun && \
    ln -sf /usr/local/bun/bin/bun /usr/local/bin/bun && \
    ln -sf /usr/local/bun/bin/bunx /usr/local/bin/bunx

# ── 安装 opencode（pin 版本）─────────────────────────────────────────────────
# 使用 npm 全局安装指定版本；opencode 包含 oh-my-openagent 插件加载能力
ARG OPENCODE_VERSION=1.14.30
RUN bun install -g opencode-ai@${OPENCODE_VERSION} && \
    # 验证安装
    opencode --version

# ── 预缓存 oh-my-openagent 插件（pin 版本）───────────────────────────────────
# opencode 在 ~/.cache/opencode/packages/ 中查找插件包；
# 预下载固定版本避免运行时联网（运行时已禁止外网）
ARG OHMY_VERSION=3.17.2
RUN mkdir -p /root/.cache/opencode/packages && \
    cd /root/.cache/opencode/packages && \
    bun add oh-my-openagent@${OHMY_VERSION} --no-save 2>/dev/null || \
    npm pack oh-my-openagent@${OHMY_VERSION} && \
    # 创建 oh-my-openagent@latest 符号链接（opencode 按 tag 查找）
    ln -sf /root/.cache/opencode/packages/node_modules/oh-my-openagent \
           /root/.cache/opencode/packages/oh-my-openagent@latest || true

# ── 将缓存目录移交给 sandbox 用户 ─────────────────────────────────────────────
RUN cp -r /root/.cache /home/sandbox/.cache && \
    chown -R sandbox:sandbox /home/sandbox/.cache && \
    # opencode 配置目录
    mkdir -p /home/sandbox/.config/opencode && \
    chown -R sandbox:sandbox /home/sandbox/.config

# ── 工作区挂载点 ───────────────────────────────────────────────────────────────
# 由 Worker 通过 docker run -v 绑定挂载，容器内仅需目录存在
RUN mkdir -p /workspace && chown sandbox:sandbox /workspace

# ── 容器入口脚本 ───────────────────────────────────────────────────────────────
COPY docker/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── 切换到非 root 用户 ─────────────────────────────────────────────────────────
USER sandbox
WORKDIR /workspace

# opencode serve 默认端口
EXPOSE 4096

# 禁止自动更新（也通过 docker run --env 注入，双层保障）
ENV OPENCODE_DISABLE_AUTOUPDATE=1

ENTRYPOINT ["/entrypoint.sh"]
