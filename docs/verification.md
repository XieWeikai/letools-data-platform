# 验证记录：平台 0.2.0

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

Provider0.6.2的完整125条严格检查、转换114条通过项为269.509秒；对照0.6.1为572.020秒。该验收使用CLI/API显式valid-only选择策略，网页仍是全部选中项必须通过。视频体积约3.88倍、采样峰值RSS约8.13GB；未采用CUVID硬解。详见[第二阶段实验](https://github.com/XieWeikai/data-providers/blob/main/tianji_provider/docs/PERFORMANCE_ROUND2.md)。

本次人工浏览覆盖上述路径；不承诺任意上游更新、所有浏览器、所有数据集或硬件组合。Hub上传只覆盖隔离mock边界；推送需要用户提供有效权限与明确目标。可复现自动测试和后续接入要求见[开发指南](development.md)。
