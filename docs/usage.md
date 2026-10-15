# 使用手册

适用于平台0.5.0、Tianji provider0.6.3；最后核验2026-10-15。先确认[部署检查](deployment.md#启动与验证)已通过。页面里的路径全部是服务器路径；本机 Downloads 目录不会因为在浏览器填写而自动上传到 Marvin。

新增“原始数据”标签页，可直接打开已保存 MCAP 轨迹，自选 topic/字段、同步播放四路相机和带竖线游标的信号曲线。原始查看不要求 check 通过，不执行转换预处理，详见[原始查看器说明](raw-viewer.md)。

0.5.2 在不支持防覆盖rename的NFS上也输出真实目录，不使用符号链接。目标会带 `.dwp-incomplete` 直到所有文件就绪；发布完成前不要使用，平台也会拒绝读取。成功后真实文件就在指定路径下，暂存会清理，直接备份/移动该目录即可。已存在的目标仍不会被覆盖；失败时完整暂存和未完成目标都保留，不自动断点续传或覆盖。

## 转换工作台

### 选择 provider 和原始轨迹

1. 打开8095首页，确认右上角已连接服务器。点击“运行环境”可查看实际 Python、LeTools 源码位置、provider来源/版本和Viewer状态。
2. provider选择`tianji`。列表来自配置的LeTools环境；lerobot/hdf5/agilex等条目可能显示但禁用，因为目前只有Tianji的原始录制选择和表单适配。
3. 点击“选择目录”，在服务器目录中进入录制父目录，或直接填写绝对路径后“加载录制”。例如Marvin的`/home/user/data_recorded/pick_and_place`。
4. 勾选需要的录制。全选针对筛选结果，清空移除当前选择；可按名称过滤、分页、逐条选择。`my_bag-*`或带data/video目录的可识别不完整录制也会显示，不能仅凭列表存在判断有效。

原始/输出路径输入框及目录弹窗会在输入后约220ms刷新匹配。完整已有目录显示子目录，最后一段输入一半时显示名称前缀匹配项；旧响应不会覆盖新输入。部分路径不能误选成它的父目录。输出选择器（包括Editor）支持“新建分类子目录”，创建后进入；最终数据集名称另外填写，由转换/编辑发布时创建。不能覆盖同名目录，也不能在原录制、缓存或数据集内部新建。

所有参数旁的浅色问号支持鼠标悬浮、键盘Tab聚焦和点击；Escape关闭。说明写明对检查、筛选、对齐和输出的影响。Doctor检查/修复选项、Editor参数同样提供帮助。

典型原始结构：

```text
pick_and_place/
└── my_bag-.../
    ├── data/metadata.yaml
    ├── data/*.mcap
    └── video/<video-bag>/metadata.yaml + *.mcap
```

不要选择已含`meta/info.json`的LeRobot数据集或`tianji-cache.json`准备缓存作为原始数采目录。录制应先保存并关闭；session仍在录制或处理时会被拒绝。

### 设置参数

| 页面参数 | 默认 | 含义 / 边界 |
|---|---|---|
| 任务指令 | `Plug in the Ethernet cable` | 必填，提交前改成真实任务；同次转换共用一个task，不自动识别动作语义 |
| FPS | 50 | 主行/视频输出频率。不是各传感器的原始频率；提高它不会创造真实相机曝光 |
| LeRobot格式 | v2.1 | v2.1按episode组织文件；v3.0分片存储，内容语义一致 |
| 输出目录 | 页面生成建议路径 | 必须尚不存在，并与源目录分离；选父目录后还须确定新的目录名 |
| Wrench频率 | 200 Hz | 1–1000，必须≥主FPS且为其整数倍；每行窗口shape=[wrench_fps/fps,12]，不滤波，窗口每点检查缺口 |
| 视频编码器 | auto，高画质H.264 | 可选auto/libx264/h264_nvenc；软件CRF18或硬件CQ16/Main；原尺寸、yuv420p。auto探测四路编码器，运行中硬件codec故障还会保留失败缓存、整批重试软件一次（进度重来、耗时增加）。显式NVENC不回退；磁盘/源数据错误不走回退。不开放任意CQ/CRF |
| 观测最大间隔 | 100ms | 反馈/视频等真实观测的严格缺口及帧年龄阈值，不是命令保持时间 |
| 连续状态重采样 | lowpass | 连续状态低通后重采样；linear使用线性插值。不能用此选项改变wrench的原始因果取样 |
| 关节命令保持阈值 | 100ms | 有效过去命令允许的年龄；超时则相应action补同帧state |
| 夹爪命令保持阈值 | 150ms | 对应provider的`gripper_max_gap_ms`；控制命令年龄，不是放宽夹爪反馈缺口 |
| 首尾静止裁剪 | 开启 | 只裁开头和结尾；保留中间暂停，一条录制仍是一条episode |
| 关节/夹爪运动阈值 | 0.002rad / 0.01 | 判定首尾运动的阈值；夹爪值为归一化电机行程 |
| 数据workers / 视频workers | 2 / 4 | LeTools封装阶段的并发，各1–8；不直接控制provider的准备/编码并发 |
| Provider workers | 留空自动 | 显式1–64；控制检查/融合准备的录制进程，NVENC仍最多12条同时编码；不改变数据规则 |
| 仅检查完整视频解码 | 开启 | 关闭可快查，但不能证明后续视频帧健康；正式转换始终完整解码 |
| gap_policy / preset | error / thor-v1（固定） | 唯一合法观测缺口策略和已验证录制契约，无忽略/拆分选项 |
| prepare_valid_only | True（平台固定） | 检查全选，只输出通过项；全异常与运行故障仍停止 |
| _collect_image_stats / _workers | 平台管理 | 融合路线保留完整图像统计；缓存Source不使用直接原始Source的内部并发，显示原因但不可无效调参 |
| report / cache / progress | 平台管理 | 按任务ID保存报告和缓存、回调驱动进度，不能写任意路径覆盖输入 |

例如50/200Hz输出[4,12]，50/400Hz输出[8,12]。改变wrench频率会重新取样和检查窗口，可能改变首个完整窗口边界，不能从旧低频输出恢复高频力信息。所有12个TianjiConfig字段均在主表单/高级参数中显示；内部/固定项解释其约束，不提供不生效的输入。详细算法见[provider参数说明](https://github.com/XieWeikai/data-providers/blob/main/tianji_provider/docs/PARAMETERS.md)。

### 检查什么、如何理解报告

“仅检查所选轨迹”读取原始数据，默认做完整视频检查，可产生JSON报告，但不编码准备缓存或最终数据集。关闭完整解码后只进行快速视频检查，结果明确标注不能证明所有视频帧健康。完整检查覆盖：文件/schema/数值、时间戳、夹爪标定、坐标系、共同有效区间、命令覆盖、观测缺口、wrench窗口、视频包/解码帧映射、尺寸和帧年龄。具体条件以[provider检查文档](https://github.com/XieWeikai/data-providers/blob/main/tianji_provider/docs/CHECKS.md)为准。

| 报告现象 | 含义 | 下一步 |
|---|---|---|
| PASS，同时有命令补齐info | 某路控制未发布/过期，已按规则使用同帧state；其他控制至少一路有效 | 查看任务是否符合单臂/暂停语义，可正常继续 |
| stationary_edges_trimmed | 按参数裁去静止首尾，属于处理说明 | 核对裁剪范围和保留帧数 |
| observation_gap / video_gap | 实际反馈或视频缺失超过阈值 | 检查采集质量；取消该条选择或明确决定更改规则后再查 |
| command_coverage_gap / alignment_error | 可能没有任何新鲜控制、无法对齐或全静止等 | 展开原始错误和信号范围，不把所有异常都归因于单臂没动 |
| video_read/decode、schema、标定、坐标系错误 | 文件或录制契约不满足 | 修复采集/输入，不依靠提高时间阈值绕过 |

页面显示topic、实测间隔、阈值、受影响帧和补齐/拒绝策略；“完整统计与对齐信息”给出进一步追溯字段。下载JSON报告保留参数与结果。检查报告是那次输入和参数的结果，不是一个以后永久有效的转换许可证。

异常和说明增加“含义、本条数据、影响、建议”。观测缺口、视频帧年龄、全路控制失效、标定/坐标系/时间戳、解码失败等分别解释；原始英文诊断可展开，code/severity/valid不变。输出验证和Doctor的WARN/FAIL也有补充解释，未知模式明确提示仍需复核，不自动生成没有证据的根因。

### 正式转换与进度

“检查并转换通过项”重新使用同一配置执行：

1. **检查与准备**：`check_and_prepare()`共享数值读取、对齐和完整视频帧流，执行全部原检查并准备缓存。每条视频输出区间之外的尾部也继续检查。
2. **生成数据集**：LeTools把缓存写成v2.1或v3.0；编码视频直接复用。
3. **验证输出**：对私有暂存目标执行deep validation。
4. **发布输出**：原子防覆盖发布到用户填写的新目录，成功后登记数据集。

全部选中项仍接受严格检查，网页显式使用provider的`prepare_valid_only=True`，只转换通过项；有异常的成功任务标为“已完成 · 跳过异常”，逐条保留跳过原因。全部异常则失败且不生成目标数据集。编码器/磁盘故障、源文件变化、清单映射不一致和输出验证失败仍停止，不能被当作可跳过的普通数据异常。原录制不删除、不拆分。

“转换前后数据量”列出所选原始录制、异常跳过、通过项裁剪前、已发布输出的episodes数量和累计轨迹时长。原始时长取各条`data/metadata.yaml`中rosbag的duration相加，包含随后跳过的输入；不是不同录制起止日期之间的跨度。输出时长按实际`total_frames / fps`计算，包含最后一帧的显示区间。两者差异包含剔除异常、共同区间对齐和首尾静止裁剪。

原始metadata缺失或时长无效时，显示已知小计和未知条数，不按0计算完整总时长。尚未成功发布时，输出显示“尚未发布”，不把通过检查的帧数冒充最终结果。独立的“任务总耗时”是处理时间。旧历史任务没有这些统计时不事后猜测补算；旧失败仍保留原状态。

逐轨迹计数来自真实回调；耗时来自实际阶段，不提供虚构的逐帧百分比。报告按输入顺序汇总，较长轨迹可能使一段时间内计数不变。旧历史任务继续显示独立check/prepare阶段，新任务显示合并阶段。

任务状态包括排队、运行、成功、失败、取消、已中断。多个操作共用单通道队列；取消会终止对应进程组，保留诊断文件。服务重启后未完成任务标记interrupted，不自动重试。重试必须重新检查源数据并使用新的输出目录。

任务列表可多选、全选已结束任务、全不选，再“清除所选记录”。这是持久隐藏，不删除`.state/jobs/<id>`的报告/缓存或输出；重启后不会重现。运行、排队及尚未退出的取消任务不可清除，混合请求原子拒绝。已知ID仍可读取报告，已有Editor计划引用不失效；当前UI没有恢复隐藏任务按钮。

## 结果与数据集登记

成功结果自动加入“数据集”页。已有数据集可以填写服务器目录手动登记；登记只存路径和元数据，不搬移或上传文件。注册时校验v2.1/v3.0布局、引用文件和切片范围，但不是训练质量认证；坏数值仍可交给Doctor检查。

每张卡片的“移除登记”仅取消路径注册；也可以勾选多张、全选/全不选后批量移除。文件、视频、数值和标注均保留；使用原路径重新添加可恢复同一ID。清除任务历史不会顺便移除数据集登记，取消登记也不会删除对应任务。

Tianji默认输出`observation.state[16]`、`observation.action[16]`、`observation.eef[14]`、`observation.wrench_windows[4,12]`和四路`observation.image.*`。主频50Hz、力窗口200Hz，力数据不滤波；命令缺失补对应state。完整字段、单位、topic和采样语义见provider文档。训练器若默认寻找`action`或`observation.images`，需配置映射；Web不改写这些字段名称。

## Viewer与分析功能

在数据集卡片点击“查看数据集”，从左侧选择episode。四路画面和状态/动作曲线按数据时间轴显示；可以隐藏/放大相机、切换曲线、播放/暂停、回到开头及拖动时间条。显示的同步容差约0.2秒，不应把播放器拖动作为逐帧精确裁剪工具。

| Viewer页签 | 用途 | 解释限制 |
|---|---|---|
| Episodes | 浏览episode、视频和时间序列 | 页面显示的是已转换数据，不是实时控制机器人 |
| Statistics | 总轨迹/帧数、FPS、feature与相机信息 | 不等于所有训练质量检查通过 |
| Filtering | 动作变化/速度分布等分析 | 过滤界面不修改磁盘原数据，不自动删除轨迹 |
| Frames | 逐相机查看首尾画面 | 视频seek需要解码，网络加载时可能有等待 |
| Action Insights | 动作自相关与跨轨迹统计 | 推荐chunk只是启发式估计；命令补state可能提高相关性，不能据此证明时间对齐正确 |
| Annotations | 编辑语言标注atoms | 保存是持久写入，见下一节 |
| Doctor | 进入数据质量诊断 | 诊断结果可能包含真实WARN/FAIL |

Tianji的`observation.action`已由Viewer适配层识别，不重命名磁盘字段。嵌套wrench窗口仍保存在数据表中；上游通用曲线界面未承诺将多维窗口直接展成200Hz力曲线。

## Annotation：保存与导出不同

1. 先在Viewer选中正确episode，进入Annotations。
2. 按实际时间点添加/编辑语言atom，填写角色、内容、style和可选camera。persistent样式包括memory/plan/subtask/task_aug；事件样式包括interjection/vqa。
3. 检查时间戳处于当前episode且相机名存在，再点击保存。保存写当前数据集`meta/lerobot_annotations.json`，保留原MP4/Parquet。
4. 刷新/重新打开确认标注可回读。需要训练表格中的语言列时，通过下面的导出API生成新目录，将标注物化为`language_persistent`和`language_events`列。

`Save episode`保存当前轨迹，`Save dataset`保存当前加载的标注集合。Grounded VQA还可在所选相机画面上画框或点选位置，按提示填写并确认；它与普通语言atom不同，需要核对对应相机和时间戳。

当前Viewer没有可见的导出按钮。使用数据集卡片对应的`local/...`标识调用平台API，例如（替换ID和新输出路径）：

```sh
curl --fail-with-body -X POST http://127.0.0.1:8095/api/export \
  -H 'Content-Type: application/json' \
  --data '{"repo_id":"local/your-dataset-id","output_dir":"/home/user/datasets/annotated-new","copy_videos":true}'
```

导出不覆盖已有目录，可复用视频硬链接（失败则复制），`copy_videos=true`强制复制。不要对复用文件做原地媒体编辑。导出是本地新数据集操作，Hub上传是另一条需显式提供认证和新repo_id的路径；平台默认不会上传，也不在日志里打印Hub令牌。

## Doctor：检查、评分、门槛和副本处理

从数据集卡片的Doctor诊断或Viewer的Doctor入口进入。已登记数据集用本地ID选择，不直接接收任意shell命令。

| 操作 | 输入与参数 | 结果/写入 |
|---|---|---|
| check | 可选检查项、max_episodes、URDF、fail_on | 结构化PASS/WARN/FAIL报告；原数据只读 |
| score | max_episodes、drop_threshold（默认30） | 质量分及建议；不会自动丢弃录制 |
| gate | policy=act/diffusion/smolvla/pi0，可选chunk_size | 对目标训练策略的准入结果 |
| merge-check | 多个数据集；post_merge检查一个合并结果 | 兼容性检查，不执行merge |
| fix | fixes=reindex/timestamps/nan/metadata/episodes，确认和新输出 | 先复制，再只修复副本，校验后解除未完成标记 |
| trim | threshold=.01、min_active=10、keep_static、dry_run，确认和新输出 | 同步裁剪表格与所有视频，不拆episode；可能删除低活动episode |

12类check是metadata、temporal、actions、videos、statistics、episodes、consistency、training、anomalies、portability、per_episode、kinematics。没有适用URDF时运动学检查可能跳过；`max_episodes`减少检查范围，应在解释报告时保留该范围。

Doctor的质量失败可以让任务显示failed，但仍产生完整报告。这与服务崩溃不同。平台将Tianji的`observation.action`以只读`action`别名提供给actions、per_episode、training、kinematics检查及score/gate；原始字段不改名，算法和阈值不变。其他检查直接读取原数据，仍检查嵌套wrench窗口等观测。报告的compatibility字段记录适配范围；缺失命令列明确失败，已有canonical action字段不会被覆盖。已知嵌套wrench窗口在通用LeTools验证中可能出现形状warning，不能仅凭warning删除数据，应核对实际`[4,12]`值和报告范围。

fix/trim要求确认和全新目标；副本携带`.dwp-incomplete`直到验证和源稳定性检查通过，失败副本不能登记。**fix的dry-run仍先建立独立副本，只是不执行修复；trim的dry-run不创建输出且不声称已验证视频。** trim支持正整数FPS、标准单流MP4、偶数尺寸，保持视频/表格同步；它不是上游只裁表格的快捷实现，也与provider原始转换时的首尾裁剪是两次独立操作。

## Editor：先计划，再生成新数据集

1. 数据集卡片点击“编辑数据集”，或进入`/editor`并加载数据集。
2. 按**原始episode ID**单条修改task，或勾选多条后批量设置task/从输出移除轨迹。仅勾选不会删除；可撤销所选编辑，也可导入`{"0":"Pick up the block"}`形式的任务映射。
3. 在“字段与相机”选择从输出移除的字段。系统时间/索引字段不可删除；删除相机同时移除其视频与统计。
4. 如需视频变换，开启“重新编码视频/调整分辨率”。选择H.264/MJPEG、精确宽高和相机；相机不勾选表示全部保留相机。
5. 指定新的输出目录，先“预览编辑计划”。检查保留轨迹/帧数、复用/重写/转码数量、最终并发和警告。计划是只读队列任务。
6. 参数未变且源文件仍相同，才能“执行编辑并生成新数据集”。后台重新计划、写暂存、验证、检查源稳定性并发布；成功后自动登记。

| 编辑参数 | 默认/范围 | 意义 |
|---|---|---|
| 宽高 | 同时留空保留原尺寸；2–8192 | 精确缩放可能改变长宽比；420格式要求宽高偶数，422要求宽度偶数 |
| H.264 CRF / preset | 23 / veryfast；CRF0–51 | 越低CRF通常画质/体积越高；这是Editor的软件编码参数，不是provider的NVENC CQ |
| MJPEG quality | 2；1–31 | 越低通常画质越高；不能与CRF/preset混用 |
| 像素格式 | 编码器默认 | 可选yuv420p/422p/444p及对应yuvj格式，须与编码器/尺寸兼容 |
| 文件workers / codec threads | 留空自动；显式1–8 | 使用Editor自身资源规划，与provider/LeTools convert并发不同 |
| Parquet批次行数 | 65536；1–1048576 | 表格分批处理资源参数，不改变采样率 |

编辑保持输入LeRobot版本、FPS和保留episode的长度。不提供任意帧区间裁剪、改FPS或原地覆盖。删除v3中间episode可能触发安全重封装或转码，以计划为准。当前Editor拒绝含符号链接的数据集；自定义根/附属文件以及已有Annotation标注不复制到输出，仍保留在源数据集。

## 常见问题

- **为什么选择目录后没有录制？** 检查这是服务器上的原始录制目录、位于白名单内，并且直接子目录符合Tianji录制结构。不要选本机路径或最终LeRobot输出。
- **单臂未动为何还有检查info？** info说明补state或裁剪；error才拦截。至少一路控制有效规则仍适用，真实反馈/视频缺失不会自动填补。
- **改FPS后失败？** 主FPS与可设置的wrench频率必须满足整数倍关系；表单和服务端都会校验。
- **为什么转换视频更大？** NVENC CQ16保留画质余量，实测体积较x264 CRF18大；可在高级参数选择libx264，画质尺度与耗时不同。
- **为什么不能覆盖输出？** 新目录是发布与恢复边界。核验已有输出后另取新目录，平台不替用户清理或覆盖数据。
- **为什么编辑按钮不可执行？** 先完成计划；改变参数、输出目录或源数据后必须重新加载/预览。
- **Viewer不可用但API正常？** 查看environment的viewer.error、Bun路径、构建缓存及服务日志；服务启动不自动build。
- **更新插件后仍显示旧版本？** 确认安装进同一venv、重装包元数据并在空闲时重启服务；不要修改全局安装。
