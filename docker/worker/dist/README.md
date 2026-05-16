# docker/worker/dist/ — 离线构建材料

此目录存放 Dockerfile 构建时所需的预下载包。  
由于开发/CI 环境无法从 DockerHub/npm 直接拉取（见 ADR-002），
所有构建材料须预先在宿主机准备并提交到此目录。

## 当前文件

| 文件 | 版本 | 来源 | 生成命令 |
|------|------|------|---------|
| `opencode-linux-x64-1.15.0.tgz` | opencode 1.15.0 | npm registry | `npm pack opencode-linux-x64@1.15.0 --registry=https://registry.npmjs.org` |
| `opencode-linux-arm64-1.15.0.tgz` | opencode 1.15.0 | npm registry | `npm pack opencode-linux-arm64@1.15.0 --registry=https://registry.npmjs.org` |
| `oh-my-openagent-cache.tar.gz` | oh-my-openagent 4.1.2 | npm registry | 见下方脚本 |

## 更新方法

升级版本时，在宿主机（有 npm 访问权限）执行：

```bash
# 1. 更新 opencode linux-x64 二进制
npm pack opencode-linux-x64@<NEW_VERSION> --registry=https://registry.npmjs.org
mv opencode-linux-x64-<NEW_VERSION>.tgz docker/worker/dist/

# 2. 更新 opencode linux-arm64 二进制
npm pack opencode-linux-arm64@<NEW_VERSION> --registry=https://registry.npmjs.org
mv opencode-linux-arm64-<NEW_VERSION>.tgz docker/worker/dist/

# 3. 更新 oh-my-openagent 插件 cache
mkdir -p /tmp/ohmy-build/oh-my-openagent@latest
cd /tmp/ohmy-build/oh-my-openagent@latest
printf '{"name":"oh-my-openagent-cache","version":"1.0.0","private":true,"dependencies":{"oh-my-openagent":"<NEW_VERSION>"}}\n' > package.json
npm install --registry=https://registry.npmjs.org --omit=dev --omit=optional --ignore-scripts
rm -f package-lock.json
cd /tmp/ohmy-build
tar czf /path/to/docker/worker/dist/oh-my-openagent-cache.tar.gz oh-my-openagent@latest/
```

## .gitignore

`*.tgz` 和 `*.tar.gz` 因体积较大已在根 `.gitignore` 中排除。  
团队成员应按上方命令在本地重新生成，或从内部 artifact store 下载。
