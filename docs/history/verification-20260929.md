# 2026-09-29历史验收

本页保留初始版本的证据，登录门禁、固定CRF18、测试数量与数据样本均属于当时环境，不代表当前行为。当前版本请看[验收记录](../verification.md)和[使用手册](../usage.md)。当时的部署清单见仓库初始提交15ff8eb；当前deploy/sources.json已随版本更新。

验收日期：2026-09-29；主机：Marvin Thor / Linux aarch64。

源码版本与 Python 依赖版本分别记录于 `deploy/sources.json`、`deploy/python-constraints.txt`。本次仅写入迁移后的独立环境和本项目，不改全局 LeTools。

## 自动化回归

- Web 平台全套：**134 passed**，包括认证、路径边界、队列与取消、原子防覆盖发布、Viewer/标注、Doctor、动作字段兼容与状态目录权限。
- LeTools 相关回归：**49 passed**，覆盖 roundtrip、video、video options、external source providers、Doctor、Visualizer。
- Viewer 适配层前端逻辑与真实上游 loader 集成：**19 passed**；Next production build、独立 TypeScript typecheck 成功。
- Python 测试有一条 Starlette 对 httpx TestClient 的弃用提示，不影响本次运行；后续升级依赖时需跟进测试客户端迁移。

## 实际转换

通过认证后的 Web API 调用真实 Tianji provider / LeTools，不使用转换 mock。

| 测试 | 结果 |
| --- | --- |
| 已知 100.85 ms 反馈缺口的录制 | 严格检查失败，未 prepare / 转换、未删除或拆分原始录制 |
| 正常录制 → v2.1 | 成功；1 episode / 988 帧 / 50 FPS；包含深度验证，总耗时 108.67 秒 |
| 同一正常录制 → v3.0 | 成功；1 episode / 988 帧 / 50 FPS；包含深度验证，总耗时 84.09 秒 |
| 加入最终原子防覆盖发布后重测 v3.0 | 成功；总耗时 83.68 秒 |

测试 task 为 `Plug in the Ethernet cable`。四路输出保持 960×540，H.264、CRF 18。以上是单条短录制的实测，不是整批吞吐保证。

服务器证据：

- `.state/verification-20260929-113537/summary.json`
- `.state/verification-20260929-121215/summary.json`

## 浏览器

真实 Chromium / Playwright，通过 SSH 隧道访问部署服务：

- 目录选择、轨迹全选/全不选/单选、桌面及 375px 窄屏布局。
- 1 个 v2.1、2 个 v3.0 数据集的四路视频播放时间推进、解码帧增加、回到开头及中段 seek。
- Annotation 新增、界面保存、API 回读、刷新回读；测试后恢复原始 atoms 并核对。
- 三份标注导出均包含 988 帧，逐行检查 Parquet 均含测试标注；重复目标拒绝覆盖。
- Viewer 和平台 Doctor 入口可用。
- 补测 v2.1 / v3.0 的 Statistics、Filtering、Frames、Action Insights：元信息与四路相机尺寸匹配；16 维真实动作有有限的速度分布和自相关曲线；Frames 首尾跳转确实推进视频解码。单样例不足两条 episode 时，跨轨迹方差按上游规则提示样本不足，不伪造统计。
- 本地 v3 使用真实 episode 分片清单，不试探缺失文件；Tianji 原始 `observation.action` 被 Viewer 正确识别，磁盘字段和值不变。
- 界面发起严格异常检查，最终展示左右夹爪反馈约 100.85 ms 的缺口、各 5 个受影响帧，以及独立的命令暂停补齐信息；补测复用该任务，没有重复转换或删除原始录制。
- 两份完整浏览器验收脚本退出码 0，page errors / console errors / HTTP failures 均为 0。

注意：原版播放器采用约 0.2 秒同步容差，验收不声称拖动条提供逐帧精确 seek。测试脚本为 `scripts/browser_smoke.cjs`、`scripts/browser_features_smoke.cjs`。

最终浏览器报告与截图已归档到服务器 `.state/browser-verification-20260929-HB642n/`，其中主流程为 `summary.json`，补测为 `features/summary.json`。包含数据内容，目录保持私有且不提交 Git。

## Doctor

通过认证 TestClient 和真实 JobManager 子进程执行 15 个作业，覆盖两种格式的 check、score、gate、merge-check（含 post-merge）、fix、trim、trim dry-run。

- 4 个新输出深度验证及自动登记成功。
- 4 路视频逐帧解码核验：960×540、988 帧、50 FPS，PTS / 帧数 / 元数据一致。
- 私有输入快照全部文件 SHA-256 及目录指纹前后相同。
- 拒绝覆盖、嵌套输出及缺少明确确认的修改操作。
- 修复实测约 0.94 / 0.97 秒；同步裁剪含重编码、验证约 124.34 / 125.04 秒。
- 原样本已经裁过静止首尾，因此保留全部 988 帧；实际删去首尾、删除静止 episode、v3 共用视频分片及 B 帧精确切片另有视频回归 fixture，覆盖 72 → 20 帧的情况。

服务器证据：`.state/doctor-verification-20260929-122128-vi6rtq2p/summary.json`。可用 `scripts/smoke_doctor.py` 复跑，每次使用独立快照与新目标。

数据质量的 FAIL / WARN 与作业崩溃不同；没有 URDF 时运动学检查按上游规则跳过。首次并行测试曾在标注元数据变化时正确拒绝发布 trim 输出，未完成标记仍保留；后续验收使用私有输入快照避免相互影响。

随后补充并验收了 Tianji `observation.action` 的只读兼容层：上游 Training Readiness / score / gate 得到明确标注的 `action` 视图，原字段和值保持不变。两种版本的 check / score / gate 共 6 个真实作业通过；12 项检查均为 **8 PASS / 4 WARN / 0 FAIL**，动作评分均为 **59.540984**，ACT gate 无 blocker。缺全局统计、样本量不足、夹爪零标准差或可能 clipping 等真实警告仍保留，没有调低阈值。源文件全量 SHA-256 前后相同。

兼容复测证据：`.state/doctor-verification-20260929-124504-rv3hnt0w/summary.json`。

## 隔离与安全边界

- 全局 LeTools 启动器、旧 LeTools 环境启动器、全局 provider 配置及 `.bashrc` / `.profile` / `.zshrc` 的 SHA-256 与部署前一致。
- 新服务端口 8095；私有 Viewer 仅监听 127.0.0.1:3010。原有服务端口未改动。
- 最终候选于 2026-09-29 13:14:24 CST 启动，Viewer 适配指纹 `ab5a44dd54bbc22425d6278d63c3e129efee904a3e03bbfb0f9a575df61d7179`；先前缓存另行保留用于回退，旧共享服务未重启。
- 新目标碰撞、目录白名单、源/输出嵌套、异常中断、进程组取消、未完成输出拒绝登记均有回归覆盖。
- 标注写入拒绝 `meta` 或标注文件的符号链接；使用固定文件名和目录 fd 原子写，链接竞争不会使 `info.json` 被替换。标注导出与转换共用 Linux 原子防覆盖发布，空目录、非空目录、文件、符号链接竞争均拒绝覆盖。
- 本项目 `.state` 及直接管理的状态子目录权限为 `700`，token 与私有 provider 配置为 `600`；初始化也会收紧部署工具提前创建的目录，不递归改变用户数据权限。
- Hub 上传入口保留，但未向 Hugging Face 创建仓库或上传数据；需要用户明确提供认证及目标。
- 本次测试不等于任意数据、任意格式组合绝无缺陷。部署默认严格拒绝未知或不支持的输入，不用补帧、拆轨迹或静默跳过来让检查通过。
