# ──────────────────────────────────────────────────────────────────────────────
# Worker Sandbox Image
#
# 基础镜像：ubuntu:24.04（Noble，本地已有 e0f16e6366fe，ADR-002 决策）
# 包含：opencode 自包含二进制、oh-my-openagent 插件预缓存
#
# Pin 版本（更新时需同步更新 ADR-002 / ADR-006）：
#   - opencode:        1.15.0（linux-x64 自包含二进制，无需 Node.js/Bun runtime）
#   - oh-my-openagent: 4.1.2（运行时依赖随 cache 一并预置）
#
# 安全设计：
#   - 非 root 用户（uid 1000，gid 1000，username: sandbox）
#   - 根 FS 运行时设为 read-only（--read-only docker run 参数）
#   - /tmp 为 tmpfs（--tmpfs docker run 参数）
#   - 不内置任何 API key / secret（全部通过容器 env 注入）
#   - OPENCODE_DISABLE_AUTOUPDATE=1（ENV 双层保障）
#
# 构建材料（需预先在宿主机准备，见 docker/worker/dist/README.md）：
#   docker/worker/dist/opencode-linux-x64-1.15.0.tgz    # 52MB，linux-x64 二进制
#   docker/worker/dist/oh-my-openagent-cache.tar.gz     # 运行时插件 cache（含依赖）
#
# 构建命令（本机开发，ubuntu:24.04 须已 docker load）：
#   docker build -t worker-sandbox:dev --platform linux/amd64 \
#     -f docker/worker/Dockerfile .
#
# 网络限制说明：
#   当前开发环境无法从 DockerHub/npm 直接拉取，所有构建材料须离线预置。
#   见 ADR-002 "镜像来源约束" 节。
# ──────────────────────────────────────────────────────────────────────────────

FROM ubuntu:24.04

# ── 系统依赖（最小集）────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        && rm -rf /var/lib/apt/lists/*

# ── 创建非 root 用户 ──────────────────────────────────────────────────────────
# ubuntu:24.04 预置 ubuntu 用户（uid/gid=1000:1000），重命名为 sandbox
RUN usermod -l sandbox ubuntu && \
    groupmod -n sandbox ubuntu && \
    usermod -d /home/sandbox -m sandbox && \
    usermod -s /bin/bash sandbox

# ── 安装 opencode（linux-x64 自包含二进制，无需 Bun/Node runtime）────────────
# opencode-linux-x64-1.15.0.tgz 由宿主机 npm pack 生成，含 bin/opencode 二进制
COPY docker/worker/dist/opencode-linux-x64-1.15.0.tgz /tmp/opencode.tgz
RUN tar xzf /tmp/opencode.tgz -C /tmp && \
    install -m 0755 /tmp/package/bin/opencode /usr/local/bin/opencode && \
    rm -rf /tmp/opencode.tgz /tmp/package

# ── 预置 oh-my-openagent 插件 cache ──────────────────────────────────────────
# opencode 在 ~/.cache/opencode/packages/oh-my-openagent@latest/ 查找插件
# cache 结构：oh-my-openagent@latest/
#               node_modules/oh-my-openagent/  ← Bun bundle（dist/index.js）
#               package.json
COPY docker/worker/dist/oh-my-openagent-cache.tar.gz /tmp/ohmy-cache.tar.gz
RUN mkdir -p /home/sandbox/.cache/opencode/packages && \
    tar xzf /tmp/ohmy-cache.tar.gz -C /home/sandbox/.cache/opencode/packages && \
    chown -R sandbox:sandbox /home/sandbox/.cache && \
    rm -f /tmp/ohmy-cache.tar.gz

# ── opencode 配置目录和工作区挂载点 ──────────────────────────────────────────
RUN mkdir -p /home/sandbox/.config/opencode /workspace && \
    chown -R sandbox:sandbox /home/sandbox/.config && \
    chown sandbox:sandbox /workspace

# ── 容器入口脚本 ───────────────────────────────────────────────────────────────
COPY docker/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ── 切换到非 root 用户 ─────────────────────────────────────────────────────────
USER sandbox
WORKDIR /workspace

# opencode serve 默认端口
EXPOSE 4096

# 禁止自动更新（与容器 env 注入双层保障；Spike 1b 已验证有效）
ENV OPENCODE_DISABLE_AUTOUPDATE=1

ENTRYPOINT ["/entrypoint.sh"]
