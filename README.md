# LeTools Data Platform

为服务器上的机器人数据提供目录选择、严格检查、LeRobot 转换、可视化、标注、诊断和编辑界面。平台负责操作入口与任务编排；数据解析和对齐由 provider 完成，目标格式由 LeTools 写入。

**当前平台版本：0.5.2；文档核验日期：2026-10-15。** 配套 Tianji provider 0.6.3、LeTools/editor 和上游组件的精确提交见 [部署版本清单](deploy/sources.json)。Python 包与启动命令仍叫 `data-web-platform`，模块叫 `data_web_platform`，systemd 单元仍为 `data-web-platform.service`；GitHub 仓库名为 `letools-data-platform`。

0.5.2 的所有新输出使用真实目录，不创建目录符号链接。支持 `renameat2(RENAME_NOREPLACE)` 时仍原子改名；不支持的NFS上排他创建目标目录，以普通文件硬链接（不支持时排他复制）填充，最后写入 `meta/info.json` 并移除 `.dwp-incomplete`。完成前平台拒绝读取，成功后清理暂存，最终目录无需隐藏存储依赖。这个NFS分支不是单步原子改名，失败时保留源和带标记的未完成目标，不能将其当作成功输出。旧0.5.1链接需显式迁移，不能直接删除其存储。

0.5.0 新增“原始数据”标签页（`/#raw`）：选择一条 MCAP 录制，自选 topic/字段、查看四路原始相机，并沿统一时间轴播放、逐帧、跳转和缩放。曲线细竖线跟随播放；原始 topic 不裁剪、不归一化、不补 action。默认 MCAP 记录时间，可切换 Header（缺失时用记录时间），完整保留各信号的开始/结束差异。详见 [原始查看器指南](docs/raw-viewer.md)。

0.4.1 配合 provider 的四路 NVENC 探测及运行时回退：auto 遇到硬件 codec 故障时整批重试软件 H.264 一次，保留失败诊断且不混用配方。检查进度可重新开始，列表按索引替换，不重复计算录制。显式硬件请求、磁盘/源文件错误和软件重试失败仍停止；严格检查和原始数据不变。

| 文档 | 读者要完成的任务 |
|---|---|
| [部署与运维](docs/deployment.md) | 首次安装、固定源码、隔离环境、Viewer 构建、systemd、升级与恢复 |
| [使用手册](docs/usage.md) | 目录/轨迹选择、参数、检查转换、Viewer/Annotation、Doctor、Editor |
| [原始数据查看器](docs/raw-viewer.md) | 原始 topic/字段选择、四路画面、共享时间轴、时钟和缺口说明 |
| [架构与代码导航](docs/architecture.md) | 三个项目的分工、数据前后端与 Web 前后端、模块和事件协议 |
| [API 说明](docs/api.md) | 请求结构、生命周期、错误、只读操作与写入边界 |
| [开发与测试](docs/development.md) | 代码注释约定、测试层次、增加 provider、仓库维护 |
| [验收记录](docs/verification.md) | 当前版本验证与明确标记的历史测量 |

相关代码：[LeTools](https://github.com/XieWeikai/letools) · [独立 providers](https://github.com/XieWeikai/data-providers) · [本平台](https://github.com/XieWeikai/letools-data-platform)。私有仓库需要 GitHub 访问权限；平台网页可访问不代表同时拥有仓库权限。

## 已有 Marvin 部署

| 内容 | 位置 |
|---|---|
| 平台源码 | `/home/user/projects/data-web-platform` |
| LeTools 源码及专用环境 | `/home/user/projects/letools-tianji`、其 `.venv` |
| Provider 仓库 | `/home/user/projects/data-providers` |
| 页面 | `http://marvin-thor.local:8095/`，或服务器可达 IP 的 8095 端口 |
| 内部 Viewer | loopback `127.0.0.1:3010`，浏览器通过 `/viewer/` 访问 |
| 状态、任务日志、准备缓存 | 平台 `.state/`，不提交 Git |

浏览器不需要登录令牌；旧 `/login` 自动回首页。此部署使用服务账号的文件权限，适用于可信内网或 Tailscale，不是公网多用户权限系统。内部 Viewer 数据桥仍有独立令牌，用户无需读取或填写它。

公司外可先建立 SSH 隧道：

```bash
ssh -N -L 18095:127.0.0.1:8095 marvin-tailscale
# 浏览器打开 http://127.0.0.1:18095/
```

`marvin` / `marvin-tailscale` 是 SSH 别名，不一定能直接作为浏览器主机名。首次部署请按 [部署指南](docs/deployment.md)，不要对现用环境重复执行创建 venv 的步骤。

## 最短使用流程

1. 在工作台选择 `tianji`，点击“选择目录”，选择**服务器上的原始录制目录**并加载。
2. 全选、清空或手动勾选轨迹。可先运行“仅检查所选轨迹”，查看每条的错误与处理说明。
3. 填写任务指令、FPS、目标 v2.1/v3.0 和一个尚不存在的输出目录，再点“检查并转换”。
4. “检查并转换通过项”依次显示“检查与准备 → 生成数据集 → 验证输出 → 发布输出”。检查全部选中轨迹，只转换通过项；异常逐条报告并跳过。全部异常或运行故障仍停止。
5. 输出成功后自动登记到“数据集”。从卡片进入可视化与标注、Doctor 或编辑器；已有 LeRobot 数据集也可手动登记。

原始 MCAP 不会被修改、删除或拆轨迹。转换、标注导出、Doctor fix/trim、Editor 都要求新输出；**Annotation 的保存会有意写入当前数据集的标注元数据**，这是与只读浏览不同的操作。

## 功能范围

| 功能 | 当前支持 | 主要边界 |
|---|---|---|
| Provider 发现 | 来自配置的 LeTools Python 环境 | 发现插件不等于已适配网页；原始数采转换目前只支持 Tianji |
| 原始数据选择 | 实时目录前缀匹配、筛选、分页、全选/清空/多选 | 新建仅创建分类父目录，不预建最终数据集 |
| 检查与转换 | 完整检查、通过项转换、异常清单、前后episodes/时长、取消、历史 | 全异常不输出；运行故障停止；重启标记 interrupted，不自动重试 |
| Tianji 数据 | 完整TianjiConfig、主FPS默认50、wrench_fps默认200、编码器/Provider workers | 主/力频率须整倍数；固定和内部Source参数明确标为只读 |
| 界面清理 | 单个/批量取消数据集登记，批量隐藏已结束任务 | 保留数据文件、报告和缓存；活动任务不可清除 |
| 参数与诊断帮助 | 浅色问号支持悬浮/聚焦/点击，异常附含义/影响/建议 | 原始错误和严重级别保留，不推断没有证据的根因 |
| 视频 | 原尺寸、高画质 H.264；可用时 NVENC，否则 x264 | 软件解码保持原严格检查；不同编码器的 CQ/CRF 不能按数字等价 |
| Viewer | 四路视频、信号图、Statistics、Filtering、Frames、Action Insights | 播放器 seek 有容差，不是逐帧编辑器；部分分析依赖足够的 episode |
| Annotation | 语言 atoms 的保存、读取、时间戳及新目录导出 | 保存写当前数据集标注；Hub 上传是另一个显式操作 |
| Doctor | check、score、gate、merge-check、fix、trim | WARN/FAIL 可是数据结果；fix/trim 只操作新副本 |
| Editor | task、轨迹/字段移除、选定相机转码/缩放、预览计划 | 保持格式/FPS；先计划再执行；原有标注/自定义附属文件不迁移 |
| LeTools 其他功能 | CLI/API 的转换、plan、merge、compare、分布式等继续可用 | 当前工作台没有包办所有 CLI 命令；Tianji provider 不支持分布式 |

## 性能与质量

2026-10-12 在 Marvin Thor 对固定 125 条录制完整检查，转换 114 条通过项、单独报告 11 条异常：82,628 行、456 段视频，流程从 572.020 秒降到 **269.509 秒（4 分 29.5 秒）**。这是该批数据和硬件的实测，不能保证所有数据同样耗时。

该历史性能验收采用软件 H.264 解码、NVENC CQ16 编码及融合检查准备。数值、时间审计和统计与旧版相同，完整视频时序与画质抽检通过。代价是该批视频约 **3.88 倍体积**、进程树采样峰值约 **8.13 GB**。0.4.0网页默认auto，也可在高级参数显式选择软件libx264或硬件h264_nvenc；不同配方耗时不应混用。完整实验见 [provider 性能记录](https://github.com/XieWeikai/data-providers/blob/main/tianji_provider/docs/PERFORMANCE_ROUND2.md)。

## 常用运维

```bash
~/projects/letools-tianji/.venv/bin/letools providers list
curl -fsS http://127.0.0.1:8095/health
curl -fsS http://127.0.0.1:8095/api/environment
systemctl --user status data-web-platform.service
journalctl --user -u data-web-platform.service -n 100 --no-pager
```

不要修改全局 `~/.local/bin/letools`、旧 `~/projects/letools` 或全局 provider 配置。重启、重建 Viewer 或更新运行代码前先确认队列空闲。仓库不包含原始录制、转换数据、令牌、venv、生成的 Viewer、运行日志或截图数据；详细备份与恢复规则见部署指南。
