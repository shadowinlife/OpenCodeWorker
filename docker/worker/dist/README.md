# docker/worker/dist/ — 离线构建材料

此目录存放 Dockerfile 构建时所需的预下载包。  
由于开发/CI 环境无法从 DockerHub/npm 直接拉取（见 ADR-002），
所有构建材料须预先在宿主机准备并提交到此目录。

## 当前文件

| 文件 | 版本 | 来源 | 生成命令 |
|------|------|------|---------|
| `opencode-linux-x64-1.14.30.tgz` | opencode 1.14.30 | npm registry | `npm pack opencode-linux-x64@1.14.30` |
| `oh-my-openagent-cache.tar.gz` | oh-my-openagent 3.17.2 | npm registry | 见下方脚本 |

## 更新方法

升级版本时，在宿主机（有 npm 访问权限）执行：

```bash
# 1. 更新 opencode linux-x64 二进制
npm pack opencode-linux-x64@<NEW_VERSION>
mv opencode-linux-x64-<NEW_VERSION>.tgz docker/worker/dist/

# 2. 更新 oh-my-openagent 插件 cache
mkdir -p /tmp/ohmy-build/oh-my-openagent@latest/node_modules
cd /tmp/ohmy-build
# 解压新版本包到 node_modules
npm pack oh-my-openagent@<NEW_VERSION>
mkdir pkg && tar xzf oh-my-openagent-<NEW_VERSION>.tgz -C pkg
cp -r pkg/package oh-my-openagent@latest/node_modules/oh-my-openagent
printf '{"name":"oh-my-openagent-cache","version":"1.0.0","dependencies":{"oh-my-openagent":"<NEW_VERSION>"}}\n' > oh-my-openagent@latest/package.json
tar czf /path/to/docker/worker/dist/oh-my-openagent-cache.tar.gz oh-my-openagent@latest/
```

## .gitignore

`*.tgz` 和 `*.tar.gz` 因体积较大已在根 `.gitignore` 中排除。  
团队成员应按上方命令在本地重新生成，或从内部 artifact store 下载。
