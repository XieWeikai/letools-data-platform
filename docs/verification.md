# 验证记录：平台 0.5.0 / 0.4.1 / 0.4.0 / 0.3.0 / 0.2.0

## 0.5.0：原始 MCAP 同步查看器（2026-10-15）

隔离候选平台完整 Python 回归 **245 passed，9.23 秒**，保留一条原有 Starlette/httpx 弃用警告；前端纯时间轴测试 **4 passed**，JavaScript 语法通过。新增覆盖源文件/符号链接边界、正在录制拒绝、空 topic、原始单位/NaN/文本、时钟回退/重复、极值选点与真实缺口、游标消息年龄、MP4 可变时间与像素等价、HTTP Range、源变化 409、队列去重与上限、中断缓存保留及发布前源变化拦截。

真实录制：`my_bag-26-10-09-16-24-47-3f581ff0`（91,702条消息/453视频帧）和 `my_bag-26-10-10-11-04-30-e8d4323f`（86,697条/417帧），各16个topic。后一条含夹爪反馈最大间隔101.944841ms，原始查看成功，没有要求转换check通过或放宽转换规则；前一条空A臂控制topic仍明确显示为0条，不补state。首条索引及封装独立实测6.46秒；不是对任意文件的速度承诺。

两条原始视频全部870帧与预览逐帧RGB像素精确相等，真实帧PTS映射误差小于1微秒，HTTP Range返回206，源文件身份/元数据指纹前后相同。两次独立验证（包含全帧解码对照）分别19.36/21.34秒。预览H.264只重新封装，无损于原码流像素，没有使用NVENC或重新编码。

浏览器实际验证：轨迹加载/缓存重开、四路960×540 canvas、5秒跳转、逐视频帧、图表点击、Header切换、0消息topic、文本topic、夹爪反馈字段、缩放和播放至终点。所有图表竖线x坐标一致，切换时钟后立即跳转播放会等待媒体定位，避免旧0秒时间回写。桌面1440px相机与曲线并排，375px无横向溢出；已有标签页切换会暂停原始播放。浏览器未捕获新JavaScript错误。

验收根目录：Marvin `.state/raw-viewer-dev-20261015-PkdRLe/`。包含两条实录的 `acceptance.json`、日志和隔离state；原始录制、视频、截图、缓存和日志不提交Git。LeTools核心与Tianji provider保持原提交不变。

## 0.4.1：NVENC 运行故障恢复（2026-10-12）

配合 provider 0.6.3，auto 在硬件 codec 资源故障后整批重试软件一次。平台根据 completed 索引替换临时检查结果，并用最终完整报告覆盖列表，避免重试后重复计算轨迹；参数帮助和静态脚本版本更新。

两仓库合并回归 442 passed、3 个按需实录测试 skipped、1 条既有 Starlette/httpx 弃用警告，18.97 秒；JavaScript 语法通过。独立私有环境重新安装，8095 systemd 服务及 strict 环境检查通过。LeTools 核心、全局工具和机器人服务未改。

通过生产 API 原样重提失败任务 `606bcac4156a40ddb92bc54a664ef34e`，新任务 `b56f17da11244e0d90a6197516b181cf`，125 条、50/200 Hz、v2.1、auto。实际再次复现 `cuCtxCreate → CUDA_ERROR_OUT_OF_MEMORY`，约 245.43 秒后整批切换到 x264；失败尝试保留在该任务的 `cache.failed-nvenc-49n5c19v`。验收证据根目录 `.state/nvenc-repair-20261012-SzD8A4/`，原失败任务及历史数据保留。

新任务成功发布 `/home/user/datasets/tianji_pick_and_place_lerobot_v21`，指令 `Put the green cube on the other one`。检查125条，转换114条/82,628行，拒绝11条；原始合计2,034.91秒，输出1,652.56秒。服务耗时718.45秒（含硬件失败尝试），检查准备712.01秒、转换1.71秒、deep validation 2.68秒。最终单一软件H.264 CRF18配方，任务成功后自动登记；原始失败记录未改成成功。

独立验收另用46.98秒：逐条判定、所有数值/审计/统计与旧成功数据集精确一致，456段视频/330,512相机帧完整解码并核对时间戳、帧数、960×540、yuv420p和CRF18；原始录制身份及元数据快照不变。没有将两种有损编码器宣称为压缩字节或画质误差相同。

## 0.4.0：列表清理、目录匹配与参数解释（2026-10-12）

229项Python回归通过，1条既有Starlette/httpx弃用警告；工作台/Editor/help脚本语法通过。测试覆盖取消登记不改文件/可重新登记、历史隐藏重启持久化、活动任务批量拒绝、写盘失败不隐藏内存状态、目录前缀/根边界/符号链接逃逸/排他创建、完整TianjiConfig映射、频率组合校验、配置转发及原始严重级别保持。

真实样本：正常录制`my_bag-26-10-09-16-24-47-3f581ff0`与异常录制`my_bag-26-10-10-11-04-30-e8d4323f`。通过API设置主50Hz、wrench400Hz、provider_workers=2、libx264、deep_video=False；仅检查确实不完整解码，转换仍完整视频检查。转换1条通过项得到659行，力数组实际形状`[659,8,12]`且全部有限，编码清单为libx264、2workers，原录制指纹不变。

隔离浏览器实际操作：单卡片移除、全选/全不选/批量取消登记、原路径重新添加；任务单选与全选清除；目录输入`.../pi`匹配pick_and_place、主输入框建议、新建分类父目录。清理后列表为空，但任务status.json及输出info.json仍在。Doctor所有input/select/textarea均已附帮助；真实12项报告中的WARN显示中文含义/影响/建议，原始报告可展开。问号点击和Escape、wrench窗口预览均验证。

隔离服务重启后任务仍隐藏、重登记的数据集仍可用；Editor目录前缀匹配和新建分类子目录实测通过。从网页提交的`7b95ace94e4b4e62959d30a52f2d76f5`实际使用wrench400、provider_workers=1、deep_video=False并成功，运行时清除复选框禁用。最终诊断文案补充后49项专项回归通过。

验收根目录：Marvin `.state/workbench-040-20261012-QyXGFv`。`acceptance.json`、`pytest.xml`与独立state保留证据；测试登记和清理都在隔离服务进行，没有批量清掉生产用户的记录。

随后部署0.4.0到常驻8095服务，实际检查任务`c5c27e8918db40fe9b0ded910ecf8bcf`使用wrench400/单worker/快速检查成功，运行中清除请求被拒绝；结束后仅隐藏该新测试记录，按ID报告仍可读。临时登记本次测试输出后仅移除这一登记，原有33条可见任务与13个数据集登记完整保留，数据指纹不变。生产默认Wrench输入为200，问号提示正常；证据为`production-acceptance.json`。全局LeTools命令和配置未改，生产registry在部署前后内容哈希相同。

## 0.3.0：异常跳过与转换前后统计（2026-10-12）

0.3.0改变Web转换的选择策略：检查所有选中项，只转换通过项；下文0.2.0“混入异常整批拒绝”属于历史行为，不再代表当前转换。独立检查仍保留失败诊断。

207项Python回归通过（1条既有Starlette/httpx弃用警告）。覆盖正常、混合、全异常、运行故障、缓存映射/帧数错误、转换后计数不符、源变化、未知原始时长及摘要重启持久化。

真实验收选取下述两条正常录制，并在中间加入`my_bag-26-10-10-11-04-30-e8d4323f`。v2.1/v3.0均成功检查3条、跳过1条、输出2条/1,419帧/50FPS。两种输出的state/action/eef/wrench_windows/timestamp和索引与原先仅转换两条通过项逐值相同；视频完整解码且尺寸保持960×540。缓存映射为原输入0→episode0、原输入2→episode1，原始文件指纹未变。

| 范围 | Episodes | 累计时长 |
|---|---:|---:|
| 转换前所选输入 | 3 | 44.466980879秒 |
| 跳过异常 | 1 | 13.879717706秒 |
| 通过项裁剪前 | 2 | 30.587263173秒 |
| 发布输出 | 2 | 28.38秒（1,419 / 50） |

只选异常录制时失败且无输出；仅检查混合选择时仍保留完整失败报告。验收证据在Marvin `.state/valid-only-20261012-ac645E/acceptance/acceptance.json`。真实网页显示“已完成 · 跳过异常”、前后统计表和逐条拒绝原因；没有将检查报告中的error改成warning或放宽阈值。

同日升级常驻8095服务至0.3.0，重新实际运行混合转换`6b125283694a449884273f7687f5042f`、全异常拒绝`b2d55a33625d45d5a7d956912ce7ea3c`和独立检查`be79e41a4e4a4692927be1538f7666d8`，三者均符合预期。生产网页重新核对前后统计与异常清单；证据在同一验收根目录的`production/acceptance.json`。旧失败任务仍保留原状态，没有重写历史报告。全局LeTools命令及provider配置的SHA-256保持不变。

## 0.2.0：原发布验收

核验日期：2026-10-12。此页记录本次源码整理与发布验收；[2026-09-29 的历史记录](history/verification-20260929.md)单独保留，不代表当前登录方式、provider版本或转换阶段。

## 干净安装与自动回归

在 Marvin Ubuntu 24.04.3 / aarch64 上新建 Python 3.12.3 venv，按部署清单安装平台、LeTools、Tianji provider和Editor；未复用生产venv的site-packages。固定源码见`deploy/sources.json`，Python依赖约束见`deploy/python-constraints.txt`。

| 验证 | 结果与范围 |
|---|---|
| 环境预检 | `scripts/check_environment.py --strict`通过；模块来自专用环境，provider0.6.2、Editor0.1.0可用 |
| Python完整回归 | 196 passed，9.04秒；1条既有Starlette/httpx弃用警告 |
| Bun适配测试 | 19 passed、54 assertions；Doctor修复后又在新构建副本上运行6项实际上游loader集成测试，全部通过 |
| Viewer安装与构建 | Bun1.4.2，frozen lockfile安装与Next.js production build成功；无Google字体下载依赖 |
| 嵌套Git缓存 | 真实`git apply`回归确认仅改缓存、不改父仓库；旧伪成功marker被发现并强制修复；仍缺本地Doctor锚点会拒绝构建 |
| Doctor多频率字段 | v2.1/v3.0含`wrench_windows[4,12]`完整12项检查；action别名不覆盖既有字段，NaN与缺失命令仍失败 |

Python覆盖路径边界、请求模型、队列、取消/重启、源变化、无登录与同源约束、严格融合转换、原子发布、Annotation保存/导出、Doctor只读兼容与副本处理、Editor计划/执行及真实视频变换。测试通过不替代下列真实录制验收。

## 真实原始数据与两个目标版本

隔离验收状态目录为`/home/user/projects/data-web-platform/.state/publication-20261012-US9tLD`。该目录是部署过程的证据，**不是仓库运行依赖**；新部署按手册生成自己的状态和样本。

原始目录：`/home/user/data_recorded/pick_and_place`，只读选取：

- `my_bag-26-10-09-16-24-47-3f581ff0`
- `my_bag-26-10-10-11-32-06-8eed0a15`

| 操作 | 真实结果 |
|---|---|
| 仅检查两条 | 2/2通过，输出诊断报告 |
| 转v2.1 | 新目录`state/demo-v21`；2条、1,419帧、50FPS、四路H.264、wrench窗口[4,12] |
| 转v3.0 | 新目录`state/demo-v30`；同样2条、1,419帧和字段；分片格式 |
| 混入坏录制 | 加入`my_bag-26-10-10-11-04-30-e8d4323f`后，1/2异常，停在check_prepare；无成功manifest、无最终目标 |
| Doctor | 两种格式均完整返回12项：8 PASS / 4 WARN / 0 FAIL；两条score正常，ACT gate通过；源指纹不变 |

WARN包含常值夹爪/动作维度、无聚合`stats.json`等原生诊断。v2.1可有逐episode统计，Doctor关于聚合统计的警告不等同于所有统计丢失。没有适用URDF时kinematics返回跳过说明；不声称做过真实机器人运动学验证。

## 浏览器与编辑回读

用实际浏览器访问隔离API18096（SSH本地转发18796）与Viewer13011，保存真实截图供使用手册。截图中的测试目录、样本任务和端口用于演示，生产入口仍是8095。

- 工作台：服务器目录选择、125条录制列表、过滤/勾选、参数、异常展开、真实四阶段进度和JSON报告。
- 数据集：v2.1/v3.0登记卡片、Viewer/Doctor/Editor入口。
- Viewer：四路视频解码和播放、Statistics、Filtering、Frames、Action Insights。Doctor iframe实际地址为本地`/doctor/?dataset=...`，不使用外部HF Space。
- Annotation：在示例episode0添加并保存`DEMO: Grasp the green block`；API重新读取成功，`/api/export`生成独立新目录并核对语言列，原数据集指纹不变。保存前的首次DEMO写入是有意的标注元数据变更。
- Editor：实际点击预览和执行，将2条输入保留1条659帧，修改task，只将head_left转为480×270、H.264 CRF18；其他三相机保持960×540。四视频全部解码659帧，state/action/eef/wrench_windows/timestamp与对应原episode逐值相同。
- Editor不会复制已有Annotation sidecar；本次确认源标注仍在、输出不含该sidecar。预览0.8秒、执行3.3秒是此小样本的测量，不是通用性能承诺。

独立结果保存在验收目录的`fixtures.json`、`doctor-real-validation.json`、`editor-annotation-validation.json`及各job报告中。未经授权没有测试真实Hub上传或控制机器人。

## 常驻生产服务回归

同日将已推送的发布版本快进部署到Marvin生产checkout，专用venv中的平台安装元数据从0.1.0更新为0.2.0。保留原部署修改的Git stash与旧Viewer缓存；没有重写历史、删除源数据或替换全局工具。原`~/.local/bin/letools`和`~/.config/letools/providers.toml`的SHA-256在部署前后相同。

实际`data-web-platform.service`继续监听8095，API/Editor/Viewer就绪，源码工作树干净。重新通过常驻队列完成两条原始数据检查与v2.1转换（2条/1,419帧）；混入已知异常后整批拒绝，没有发布目标。证据在验收目录的`production-acceptance/verified.json`及其job记录。

通过SSH转发实际8095页面再次确认无需登录、provider0.6.2、四路960×540视频readyState=4且无媒体错误，Viewer Doctor iframe指向同源`/doctor/?dataset=...`。该回归覆盖重启后的常驻服务，不只是在新进程里调用函数。

## 性能与适用范围

Provider0.6.2的完整125条严格检查、转换114条通过项为269.509秒；对照0.6.1为572.020秒。该历史性能验收使用CLI/API显式valid-only，0.2.0网页当时要求全部选中项通过；0.3.0网页已使用通过项转换。视频体积约3.88倍、采样峰值RSS约8.13GB；未采用CUVID硬解。详见[第二阶段实验](https://github.com/XieWeikai/data-providers/blob/main/tianji_provider/docs/PERFORMANCE_ROUND2.md)。

本次人工浏览覆盖上述路径；不承诺任意上游更新、所有浏览器、所有数据集或硬件组合。Hub上传只覆盖隔离mock边界；推送需要用户提供有效权限与明确目标。可复现自动测试和后续接入要求见[开发指南](development.md)。
