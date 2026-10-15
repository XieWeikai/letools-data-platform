# 部署与运维

适用于平台 0.5.0、Tianji provider 0.6.3，核验日期 2026-10-15。生产验收环境是 Ubuntu 24.04.3 / Linux aarch64、Python 3.12.3、Bun 1.4.2、系统 FFmpeg 6.1.1。部署使用一个专用 Python venv，不替换任何全局工具。Linux 原子防覆盖发布是生产依赖，macOS/Windows 不是本指南承诺的服务器部署平台。

0.5.0 原始查看器使用同一私有环境中的 MCAP、NumPy、PyAV 和 Tianji schema 编译器，无需新前端构建或 GPU。衍生缓存位于 `.state/raw-viewer`，单进程后台索引与转换队列独立，服务关闭时停止其自有索引进程；源文件变化会使缓存失效。部署前也应检查是否有人正在建立原始预览，避免中断其首次索引。无须重建 LeRobot Viewer 缓存；重启后刷新页面加载 0.5.0 静态脚本。

0.5.2 的 NFS 发布兼容：NFS4 可能允许读写却不支持 renameat2 的 RENAME_NOREPLACE，原版会在验证结束后报 EINVAL。现在排他创建真实目标目录，保留 `.dwp-incomplete` 到文件全部就绪；普通文件优先硬链接、否则排他复制，meta/info.json最后写入，不使用符号链接。成功后删除本次暂存，最终目录可独立复制和使用；失败保留完整源和带标记的部分目标供排查。此分支不是原子目录改名；平台拒绝未完成标记，外部程序应在任务成功后再读。旧0.5.1符号链接仍需显式迁移。平台内核仍需要Linux，该变更不修改NFS挂载、NAS权限或用户映射。

Marvin NAS 访问采用该服务的 systemd drop-in：`~/.config/systemd/user/data-web-platform.service.d/30-nas-tianji.conf`，设置 `DWP_ALLOWED_ROOTS=/home/user:/media/user/BAG_STORAGE:/nas/tianji`。该主机配置保留旧白名单并仅增加指定 NAS 子树，不将整个 `/nas` 开放。

## 前置条件与目录

- 有源码仓库读取权限；本平台仓库为私有时，先配置自己的 GitHub/gh 登录。不要把令牌写进命令、仓库或截图。
- Git、Python >=3.12、uv、Bun、`/usr/bin/ffmpeg`；Bun 默认路径是 `~/.bun/bin/bun`，可用 `DWP_BUN` 改。
- 可选编辑器需要 Rust >=1.88 和系统 linker，仅安装/构建时使用。运行已构建的 editor 不需要启动 Rust 编译器。
- NVENC 不是安装必需条件。没有兼容 NVIDIA 硬件/驱动/PyAV 编码器时，provider auto 会在独立四路探测后使用软件 x264。Provider 0.6.3 还支持运行时硬件 codec 故障后整批重试软件一次；失败缓存和原因保留，数据检查标准不变。不要为了平台安装更改机器人驱动。
- 原始数据挂载目录要对服务账号可读；输出、`.state` 和 venv 要可写。保留视频和中间缓存所需空间，勿默认写回容量紧张的采集盘。

建议沿用现有 Marvin 目录结构：

```text
~/projects/
├── letools-tianji/                 # LeTools 源码
│   └── .venv/                     # 平台/core/provider/editor 的同一环境
├── data-providers/
│   └── tianji_provider/
└── data-web-platform/             # clone 的仓库名是 letools-data-platform
    └── .state/                   # 私有运行数据，不属于源码
```

## 首次取得并固定源码

以下仅用于目录尚不存在的首次安装。已有部署跳到“升级”。

```bash
mkdir -p "$HOME/projects"
gh repo clone XieWeikai/letools-data-platform "$HOME/projects/data-web-platform"
git clone --recurse-submodules https://github.com/XieWeikai/letools.git "$HOME/projects/letools-tianji"
git clone https://github.com/XieWeikai/data-providers.git "$HOME/projects/data-providers"
cd "$HOME/projects/data-web-platform"

LETOOLS_REV=$(python3 -c 'import json; print(json.load(open("deploy/sources.json"))["letools"]["commit"])')
PROVIDERS_REV=$(python3 -c 'import json; print(json.load(open("deploy/sources.json"))["data_providers"]["commit"])')
git -C ../letools-tianji checkout --detach "$LETOOLS_REV"
git -C ../letools-tianji submodule update --init --recursive
git -C ../data-providers checkout --detach "$PROVIDERS_REV"
```

固定提交是为了可复现，不是要求把所有仓库强行 reset。修改过的现有 checkout 先检查并保存工作，不能直接覆盖。`deploy/sources.json` 同时记录绑定的 Viewer/Doctor 子模块版本；它们由 LeTools 管理，不单独追上游 main。

## 创建专用环境并安装四个项目

先核对工具可用：

```bash
python3.12 --version
uv --version
"${DWP_BUN:-$HOME/.bun/bin/bun}" --version
/usr/bin/ffmpeg -version
rustc --version
```

没有 Rust 时，可按 [LeTools Editor 安装要求](https://github.com/XieWeikai/letools/blob/main/docs/EDITOR.md) 准备工具链。Marvin 已有私有工具链，启用它只影响当前 shell：

```bash
export CARGO_HOME="$HOME/projects/data-web-platform/.state/editor-toolchain/cargo"
export RUSTUP_HOME="$HOME/projects/data-web-platform/.state/editor-toolchain/rustup"
export PATH="$CARGO_HOME/bin:$PATH"
rustc --version
```

新主机不要只执行上述 export 就假设 Rust 已安装；确认命令实际输出 >=1.88，再构建 editor。无需把这些环境变量写入共享 shell 配置。

```bash
cd "$HOME/projects/data-web-platform"
uv venv --python python3.12 "$HOME/projects/letools-tianji/.venv"
LETOOLS_PY="$HOME/projects/letools-tianji/.venv/bin/python"
uv pip install --python "$LETOOLS_PY" \
  --index https://xieweikai.github.io/letools/simple --index https://pypi.org/simple \
  --index-strategy unsafe-best-match -c deploy/python-constraints.txt \
  -e ../letools-tianji -e ../data-providers/tianji_provider \
  -e ../letools-tianji/packages/letools-editor -e . pytest
"$LETOOLS_PY" scripts/install_isolation.py
"$LETOOLS_PY" scripts/check_environment.py --strict
"$HOME/projects/letools-tianji/.venv/bin/letools" providers list
```

包安装必须使用这里的 `--python`。不使用 `uv tool install --force`，也不在活跃环境执行仅保留 core 的 `uv sync`，后者可能移除可选 editor/provider。私有 native wheel 索引只用于此安装命令；依赖快照是已验收 Linux 环境，不是所有系统的通用锁文件。`opencv-python-headless` 是当前 provider 的实际依赖，不能漏装。

`install_isolation.py` 拒绝全局解释器，只在当前 venv 写受管理的 `.pth` 和独立 provider 配置。它保留显式 `LETOOLS_PROVIDERS_FILE` 设置；Web 子进程另使用 `.state/config/providers.toml`。不要复制另一份全局配置的源码模块路径。

## 构建 Viewer

```bash
cd "$HOME/projects/data-web-platform"
"$HOME/projects/letools-tianji/.venv/bin/python" setup_viewer.py
```

它把 LeTools 绑定的完整 Viewer 复制到平台 `.state/viewer-cache/source`，在副本上应用同源数据/标注适配，然后用 Bun frozen lockfile 安装与 production build。原 LeTools 源码和子模块不修改；服务启动不会偷偷下载或构建前端。

`--no-install` 仅用于依赖已安装时跳过 Bun install；`--no-build` 不等于可运行的生产构建；`--force` 强制重建本平台缓存。不要在当前 Viewer 仍运行时重建同一个目录。可先在独立 `DWP_STATE_DIR` 构建验收，再于维护窗口切换；这会使用独立注册表/任务历史，不能把它误认为仅换缓存。

## 启动与验证

前台试运行：

```bash
cd "$HOME/projects/data-web-platform"
"$HOME/projects/letools-tianji/.venv/bin/data-web-platform"
```

默认监听 8095，Viewer 子进程仅监听 127.0.0.1:3010。确认停止前台试运行后，安装唯一的用户服务：

```bash
"$HOME/projects/letools-tianji/.venv/bin/python" scripts/install_service.py
systemctl --user status data-web-platform.service
curl -fsS http://127.0.0.1:8095/health
curl -fsS http://127.0.0.1:8095/api/environment
curl -fsS http://127.0.0.1:8095/api/editor/options
```

成功标准：`/health` 返回平台 `status: ok`；environment 的 `error` 为 null、tianji 版本为 0.6.3、Python 指向专用 venv、Viewer ready；editor/options 的 available 为 true。`/api/health` 是 Annotation 子系统健康接口，不能代替平台 `/health`。

单元模板默认固定 `~/projects/data-web-platform` / `~/projects/letools-tianji`。不同目录须先调整单元或写 systemd drop-in；仅改变 shell 环境不会改变已运行服务。若要求退出登录后仍运行，由管理员按主机策略配置 user lingering，并用 `loginctl show-user "$USER" -p Linger` 检查；用户级 unit 本身不等于已开启 lingering。

## 配置项

| 环境变量 | 默认 / 含义 |
|---|---|
| `DWP_PROJECT_ROOT` | 本平台 checkout；用于静态资源、适配层和工作目录 |
| `DWP_LETOOLS_ROOT` | `~/projects/letools-tianji`；默认 worker Python 的来源 |
| `DWP_PYTHON` | `$DWP_LETOOLS_ROOT/.venv/bin/python`；provider 发现和后台 worker 解释器 |
| `DWP_STATE_DIR` | `$DWP_PROJECT_ROOT/.state`；注册、任务、报告、令牌与 Viewer 缓存 |
| `DWP_BUN` | `~/.bun/bin/bun`；Viewer build/serve 的 Bun |
| `DWP_HOST` / `DWP_PORT` | `0.0.0.0` / `8095`；外层服务监听 |
| `DWP_VIEWER_PORT` | `3010`；内部 loopback Viewer 端口 |
| `DWP_ALLOWED_ROOTS` | 默认用户 home 与 `/media/user/BAG_STORAGE`；冒号分隔的服务器目录白名单 |
| `DWP_DEFAULT_SOURCE` | `/media/user/BAG_STORAGE/recorded_bags`；页面默认路径，不会自动转换 |

平台的一些读取/Editor 接口会在 Web 进程内导入 LeTools/editor，因此推荐四个项目装在同一 venv；不要仅更改 DWP_PYTHON 就假定 API 进程可以缺少这些依赖。`DWP_INTERNAL_TOKEN`、`DWP_DATA_ORIGIN` 由平台管理，不是用户登录配置。

## 访问方式与状态备份

页面/API 无登录，连通端口即可使用服务账号允许的功能。同源校验限制浏览器跨站写入，但不替代用户授权或网络边界。仅向可信网络开放；如需只通过隧道访问，可将 DWP_HOST 设为127.0.0.1。不要直接暴露到公网。

```bash
ssh -N -L 18095:127.0.0.1:8095 marvin-tailscale
# http://127.0.0.1:18095/
```

`.state` 含源路径、运行记录、标注导出与内部凭证；目录为私有700，关键文件600。备份必须同样保密；不要提交Git。`datasets.json` 是路径注册，备份它不等于备份数据文件；Annotation 在数据集内，需要另行备份对应数据集元数据。Viewer build 可重建，任务准备缓存可很大，清理前确认没有活任务并明确具体目录，本项目不自动删历史数据。

0.4.0的“清除任务记录”仅隐藏列表，`hidden-jobs.json`也应纳入状态备份；它不是磁盘缓存清理功能。数据集移除仅取消登记，可按原路径重新登记。原版本状态无需迁移：不存在隐藏ID文件时列表行为与原版相同。升级后刷新浏览器加载版本化静态资源；本次未更改Viewer适配代码，无需重建其缓存。

## 升级、维护窗口与恢复

1. 记录三个仓库的 HEAD、工作树状态、环境检查结果和服务配置。确认队列没有 running/queued 任务；不要把历史 interrupted 当成活任务。
2. 保留源码变更，按清单更新需要的组件，使用同一 venv 的 `uv pip install`。文档变动不需重装；包版本/依赖变化需要更新安装元数据。
3. 在隔离输出上跑测试。只有 Viewer/适配层指纹变化时才安排缓存重建。
4. 空闲时重启该服务，重读 environment，验证只读检查、正常/异常混合输入只转换通过项，以及全部异常不发布。不要动全局 LeTools 或其他 systemd 服务。
5. 如失败，先查看日志和最后成功步骤。恢复已记录源码/依赖/Viewer缓存，再验证健康及实际任务；不删除用户输出、任务证据或原数据。禁止用 `git reset --hard` 清理未知工作树。

`interrupted` 任务不会自动重试；取消会结束任务进程组，未完成缓存保留。任务显示失败但目标已存在时，可能是成功发布后的报告/同步错误：先检验该目录，再决定登记或换新目录重试。现有目标始终不会被自动覆盖。
