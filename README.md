# Data Web Platform

Marvin 上独立部署的 LeTools / Tianji 数据工作台。提供录制目录选择、轨迹多选、严格检查、prepare、LeRobot v2.1 / v3.0 转换，以及原版 Visualizer、Annotation 与 Doctor 的 Web 接入。

## 当前部署

| 内容 | 位置 |
| --- | --- |
| LeTools 源码 | `/home/user/projects/letools-tianji` |
| 独立 Python 环境 | `/home/user/projects/letools-tianji/.venv` |
| Tianji provider 源码 | `/home/user/projects/data-providers/tianji_provider` |
| 本项目 | `/home/user/projects/data-web-platform` |
| Web | `http://marvin-thor.local:8095`，或使用能访问 Marvin 的 IP |
| 内部 Visualizer | `127.0.0.1:3010`，由本服务管理，通过 `/viewer/` 访问 |
| 状态、报告、构建缓存 | 本项目 `.state/`，不提交 Git |

不修改全局 `~/.local/bin/letools`、原 `~/projects/letools` 或全局 `~/.config/letools/providers.toml`。这三项不是本服务的依赖。本服务使用用户级 systemd，不需要 sudo。

### 打开与登录

本机已验证可直接访问 `http://marvin-thor.local:8095`，也可使用 `http://<Marvin 的可达 IP>:8095`。`marvin` 是 SSH 配置别名，不一定是浏览器可以解析的主机名。访问口令在服务器私有文件中，按需读取：

```sh
ssh marvin 'cat ~/projects/data-web-platform/.state/access-token'
```

口令不写入仓库、不放 URL、不保存到浏览器 localStorage；登录使用 8 小时的 HttpOnly / SameSite Cookie。不要把 8095 直接暴露到公网。HTTP 不加密，跨不可信网络请用 SSH 隧道：

```sh
ssh -N -L 18095:127.0.0.1:8095 marvin
# 浏览器打开 http://127.0.0.1:18095
```

口令是该工作台的共享访问凭证，不是多用户权限系统。使用服务的 `user` 账号权限读写允许目录。默认可浏览 `/home/user` 与 `/media/user/BAG_STORAGE`；生产使用可通过 `DWP_ALLOWED_ROOTS` 缩小范围。不要向不可信用户分发口令。

## 使用流程

1. 在转换页选择 provider。列表来自配置的 LeTools 环境，Tianji 是当前实现了 Web 参数适配的原始数采 provider。LeTools 的内置 provider 也会显示，未适配者不可选，不会被冒充为已支持。
2. 点击目录选择，浏览 **Marvin 服务器文件系统**。选择录制总目录，或一条录制目录；列表支持全选、全不选、单独勾选、筛选和分页。不完整但可识别的录制同样列出，交由检查说明异常。
3. 设置 task（必填）、FPS（默认 50）、输出 v2.1 / v3.0 和全新目标目录。当前默认 task 是 `Plug in the Ethernet cable`。高级设置包括观察/命令间隔阈值、重采样方式、首尾静止裁剪和并发数。
4. 可先点“检查”；正式转换仍重新执行 **check → prepare → convert → deep validation → publish**，避免沿用过期检查。检查逐条显示真实问题；prepare 显示编码进度，无法精确测量的阶段不伪造百分比。
5. 检查通过且输出验证通过后自动登记数据集。在数据集面板打开 Visualizer、Annotation 或 Doctor；也可以登记已有 v2.1 / v3.0 数据集。

### Tianji 对齐规则

转换调用安装的 `tianji_provider`，不在 Web 层重新实现信号对齐：使用共同有效时间范围、配置的目标时间网格、provider 的状态/视频采样与命令停发补齐规则；首尾静止裁剪可开关。观察信号的真实缺失仍严格报错，控制命令暂停不等于观察缺失。

不会自动删除异常录制、不会把一条录制拆成多条、不会为通过检查而擅自放宽阈值。遇到异常应阅读报告后取消选择该轨迹；只有用户显式调整设置才改变规则。原始数据应停止录制并保存完成；检查和准备前后会核对源文件是否变化。

视频保持 provider 的原分辨率与 H.264 高画质编码策略；LeTools 后端复用已准备的视频，不因进入 Web 流程额外降低分辨率。输出只允许新目录：转换先写私有暂存目录，深度验证后通过 Linux 原子防覆盖操作发布。失败或目标被其他人抢先创建时，不会覆盖目标，未发布输出保留用于排查。

如果已完成发布、但随后写报告或同步磁盘失败，任务仍可能显示失败，而完整目标已经存在。此时先核对目标和报告，可在数据集面板手动登记并运行 Doctor；不要删除目标后直接重试。重新转换请选择另一个新目录。

### Visualizer、Annotation、Doctor

- Visualizer 使用该 LeTools 版本绑定的完整上游 Next.js 界面，不是截图或替代播放器。页面位于 `/viewer/`，视频支持 Range/seek；浏览器与内部服务通过同源桥接读取已登记数据。
- Annotation 可按 episode 编辑、保存、读取时间戳和导出。**保存标注会写入所选数据集的标注元数据**；不会重写原 MP4 / Parquet。导出要求独立新目录。
- Doctor 支持 `check`、`score`、`gate`、`merge-check`、`fix`、`trim`，包括选择检查项、质量门槛、URDF 路径及详细报告。FAIL/WARN 是数据质量结果，不等同于服务器故障。
- Tianji 的动作字段保留为 `observation.action`。Web 层为上游 Doctor 的 Training Readiness / score / gate 提供只读 `action` 别名视图，并在报告里说明；不重命名数据、不修改原始值、不调低质量阈值，也不隐藏真实 WARN / FAIL。
- `fix` 只修改新建独立副本，不原地修复；`trim` 同步重建表格、索引、统计与视频，只写新输出。两者需要确认。原版 Doctor 的表格-only 裁剪不适用于同步训练数据，因此由本项目同步裁剪适配器替代。
- Doctor trim 依据 state/action 活动阈值裁去首尾，低活动轨迹默认丢弃（`keep_static` 可保留），不切成多个 episode；全部被丢弃则报错。此操作与 Tianji prepare 的首尾裁剪是两套明确的用户操作。
- Doctor trim 支持正整数 FPS、标准单流 MP4、偶数宽高 RGB 视频；缺帧/非连续时间轴/不支持格式会拒绝，不擅自补帧或缩放。dry-run 不创建输出，也不声称已验证视频。
- 上游 Hub 相关入口保留，但上传需显式配置认证和全新的远端仓库。部署验收不向 Hugging Face 创建或上传数据。

## 环境与维护

命令行使用本环境，不改变别人用的全局命令：

```sh
source ~/projects/letools-tianji/.venv/bin/activate
letools providers list
letools --help
# 或直接调用，不 activate：
~/projects/letools-tianji/.venv/bin/letools providers list
```

`.venv` 内的 `.pth` 只为本环境设置独立 provider 配置默认值；Web 服务另外指定自己的 `.state/config/providers.toml`。切勿把旧全局配置里的 provider 源码路径复制进来。

```sh
systemctl --user status data-web-platform.service
journalctl --user -u data-web-platform.service -n 100 --no-pager
systemctl --user restart data-web-platform.service
```

重启会中断正在进行的转换，历史状态会标记为 interrupted，不会自动重试或覆盖输出。先在页面确认没有运行任务再重启。默认一次运行一个任务，内部并发可设置，以减少与机器人和其他服务争用资源。取消会终止该任务的进程组，保留诊断信息。

### 在相同目录结构重装

先准备 `deploy/sources.json` 指定的三个源码版本及 LeTools 子模块。所有 Python 包仅进入目标 venv：

```sh
cd ~/projects/data-web-platform
~/.local/bin/uv venv --python python3.12 ~/projects/letools-tianji/.venv
~/.local/bin/uv pip install --python ~/projects/letools-tianji/.venv/bin/python \
  --index https://xieweikai.github.io/letools/simple --index https://pypi.org/simple \
  --index-strategy unsafe-best-match -c deploy/python-constraints.txt \
  -e ~/projects/letools-tianji -e ~/projects/data-providers/tianji_provider -e . pytest
~/projects/letools-tianji/.venv/bin/python scripts/install_isolation.py
~/projects/letools-tianji/.venv/bin/python setup_viewer.py
~/projects/letools-tianji/.venv/bin/python scripts/install_service.py
```

以上创建 venv 的步骤用于首次安装，不应对使用中的环境直接执行。已有环境更新使用 `uv pip install`，先跑测试，再于任务空闲时重启服务。`setup_viewer.py` 需要 Bun（默认 `~/.bun/bin/bun`）；只在 `.state` 中复制、适配、构建 Viewer，不改 LeTools 子模块源码。安装脚本在源码或适配层指纹变化时会更新缓存，`--force` 可强制重建；正常启动服务不会自动构建。重建会替换本项目的生成缓存，应先停掉本服务并自行保留缓存备份，或在独立 `DWP_STATE_DIR` 中构建、验收后切换。不要在 Viewer 正运行时直接重建同一缓存。

配置由 `DWP_PROJECT_ROOT`、`DWP_LETOOLS_ROOT`、`DWP_STATE_DIR`、`DWP_PYTHON`、`DWP_BUN`、`DWP_HOST`、`DWP_PORT`、`DWP_VIEWER_PORT`、`DWP_ALLOWED_ROOTS` 和 `DWP_DEFAULT_SOURCE` 控制；允许目录用冒号分隔。修改 systemd Environment 后需要 daemon-reload 和重启。

`.state` 可能包含大型 prepare 缓存、测试输出、源路径、日志与访问凭证，不应公开或提交。BAG_STORAGE 空间有限，默认输出建议放系统磁盘的 `~/datasets`。没有自动删除历史数据的任务。

## 测试与结构

```sh
cd ~/projects/data-web-platform
~/projects/letools-tianji/.venv/bin/python -m pytest -q
# Viewer 构建完成后运行适配层与真实上游 loader 集成测试：
DWP_VIEWER_SOURCE="$PWD/.state/viewer-cache/source" \
  ~/.bun/bin/bun test viewer_overlay/ tests/viewer-fetch-integration.test.ts
# 真实录制端到端验收，会生成独立测试输出：
~/projects/letools-tianji/.venv/bin/python scripts/smoke_pipeline.py
```

`scripts/browser_smoke.cjs` 使用 Playwright 测试实际页面；`scripts/browser_features_smoke.cjs` 另外验证 Statistics、Filtering、Frames、Action Insights 和逐条异常报告。两者需要运行服务、可达 origin 和测试数据，默认通过本地 SSH 隧道访问 Marvin，凭证仅保存在进程内。补测脚本可通过 `DWP_EXISTING_CHECK_JOB=<id>` 复用已有检查报告，避免重复读取原始录制。

架构、入口与扩展位置见 `docs/architecture.md`；实际样本、浏览器与隔离验收记录见 `docs/verification.md`。测试数据、浏览器截图和报告位于 `.state/`，不含在 Git 提交中。
