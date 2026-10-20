# HTTP API与任务契约

平台0.5.0；本服务关闭通用OpenAPI/交互schema页面，本文记录受限HTTP接口。字段的最终约束由`models.py`、`doctor.py`、`editor.py`、`raw_viewer.py`和`viewer.py`的Pydantic模型定义。未声明字段会被拒绝，不能用请求体选择任意Python模块或shell命令。

## 访问与错误

可信网络内无需浏览器登录或Cookie。带Origin的写请求必须同源；无Origin的CLI/API调用仍可使用。这不是身份认证。服务端统一处理常见无效值400、无权限403、路径缺失404；模型错误422、能力不可用503。已接受任务一般返回202和job对象，之后的异步失败从任务状态和报告读取。

示例均在服务器本机调用，不含凭证：

```bash
curl -fsS http://127.0.0.1:8095/health
curl -fsS http://127.0.0.1:8095/api/environment
curl -fsS 'http://127.0.0.1:8095/api/browse?path=/home/user/data_recorded'
```

路径参数应正常URL编码；不要将用户路径直接拼成shell表达式。所有路径是服务器路径，由服务账号的OS权限决定访问范围，无目录白名单；输入/输出分离和数据集内部引用约束仍保留。目录访问权限不足返回403，磁盘或网络挂载不可用返回503及具体说明。

## 工作台、注册与队列

| 方法 / 路径 | 行为 |
|---|---|
| GET `/health` | 平台进程健康，不等于每个可选组件都就绪 |
| GET `/api/environment` | Python/LeTools根路径、provider来源版本、`path_access_mode=os_permissions`、`service_user`、目录导航起点`roots`（不是白名单）、Viewer状态和发现错误；不探测磁盘挂载 |
| GET `/api/browse?path=...` | 目录浏览；不是任意文件内容下载 |
| GET `/api/directories/suggest?path=...` | 完整路径列子目录，末段前缀列匹配项；exact表示输入是否为已有目录 |
| POST `/api/directories` | `{"parent":"/allowed/existing","name":"new-group"}`，仅创建一个新分类子目录，同名409，不允许写进原录制/数据集/缓存 |
| GET `/api/ui-help` | 转换、Doctor、Editor及检查/修复项的说明字典 |
| GET `/api/recordings?path=...&provider=tianji` | 当前可选原始录制及不完整标记 |
| POST `/api/jobs` | 提交check或convert |
| GET `/api/jobs`、`/api/jobs/{id}` | 历史或单条状态；报告可能很大，客户端按需取单条 |
| POST `/api/jobs/{id}/cancel` | 取消排队/运行任务；终态重复调用返回现有状态 |
| GET `/api/jobs/{id}/report` | 下载完整任务JSON |
| GET / POST `/api/datasets` | 列出数据集 / 以`{"path":"/absolute/path"}`登记已有数据集 |
| POST `/api/datasets/remove` | `{"ids":["ID", "local/ID"]}`，只取消登记，返回removed与files_deleted=false，可重新登记 |
| POST `/api/jobs/clear` | `{"ids":["JOB_ID"]}`，持久隐藏已结束任务；活动项使整批原子拒绝，保留文件及按ID读取报告 |

原始转换请求：

```json
{
  "kind": "convert",
  "provider": "tianji",
  "source_root": "/home/user/data_recorded/pick_and_place",
  "recordings": ["/home/user/data_recorded/pick_and_place/my_bag-EXAMPLE"],
  "destination": "/home/user/datasets/new_pick_and_place_v21",
  "target_version": "v2.1",
  "params": {"task": "Pick and place", "fps": 50, "wrench_fps": 200},
  "workers": 2,
  "video_workers": 4,
  "provider_workers": null,
  "video_encoder": "auto",
  "deep_video": true
}
```

此处轨迹名是示例，先从recordings接口取得真实路径。check可省略destination；convert必须新目录。所有选中项必须来自当前source_root列表，不可重复。params包含完整TianjiConfig，wrench_fps默认200且须≥fps并整倍数。顶层provider_workers为null自动或1–64，video_encoder为auto/libx264/h264_nvenc，deep_video只影响独立check（融合convert始终完整检查）。prepare_valid_only固定True不接受覆盖，Source内部统计/原始并发和报告路径由平台管理。

请求没有幂等key：重复POST会创建新job，并可能因输出预留/已存在而被拒绝。提交超时先查任务记录，不要盲目重发。队列一次一个作业，退出码与result决定终态；取消保留日志/中间产物，重启标记interrupted，不自动恢复执行。

典型状态字段：id、kind、status、stage、progress、stages、elapsed_seconds、check_report、check_results、result、error、logs、request。转换最新阶段为check_prepare/convert/validate/publish；历史任务可含独立check/prepare。进度不承诺逐帧百分比。

0.4.1 配合 provider 0.6.3：video_encoder=auto 的硬件 codec 运行故障可以触发一次整批软件重试，日志和缓存清单保留原因与失败尝试。check_result 的 completed 可从 1 重新开始；任务投影按索引替换临时结果，最终 check_report 覆盖临时列表，不重复计算轨迹。显式 h264_nvenc 及源解码、磁盘、未知 worker 崩溃仍明确失败。

0.3.0转换固定检查全部输入、只转换通过项。因此`status=succeeded`可以与`check_report.valid=false`同时存在：前者表示通过子集已验证并发布，后者说明原始选择含异常。全部异常仍失败。运行故障、源变化或验证失败不属于可跳过的数据异常。

新增`conversion_summary`，成功时也放入`result.conversion_summary`。`input`、`accepted`和`skipped`含episodes、duration_seconds、known_duration_seconds、unknown_duration_episodes；任一原始时长未知则该组duration_seconds为null。skipped.recordings保留原路径、source_recording_index及issues。`output`仅发布后设置episodes、frames、fps、duration_seconds，之前为null。原始时长按数值rosbag metadata相加，输出按frames/fps；与elapsed_seconds处理耗时不同。历史记录缺字段时保留旧含义，不补算。

0.4.0 API快照为issue及Doctor WARN/FAIL附加`explanation`（title、meaning、evidence、effect、next_step），并提供`validation_explanations`/`error_explanation`。原message、code、severity、valid不改；转换worker保存的原始provider报告保持原样。说明是已审阅规则匹配，未知模式明确保留不确定性，不是自动修复或在线模型生成。

## 原始数据查看

原始数据另有只读内容 API（POST open 只建立衍生缓存）：

| 方法 / 路径 | 参数与结果 |
|---|---|
| POST `/api/raw/open` | `{"path":"/absolute/saved/recording"}`；202 返回指纹 id、queued/running/ready，ready 含 manifest；队列满429 |
| GET `/api/raw/{id}` | 状态、真实消息读取数量或完成清单；源已变化409；中断索引标记 interrupted，可重新 open |
| GET `/api/raw/{id}/series` | topic=清单中的 topic-NNN、重复 fields、clock=record/header、start/end（相对秒）、limit=100–5000；返回真实选点、值和原始缺口 breaks |
| GET `/api/raw/{id}/samples` | 重复 topics（最多32）、clock、at（相对秒）；完整索引中游标之前最近的原始消息、时间与年龄；不做插值。record_ns 是接收时间，header_or_record_ns 是 Header 模式实际采用的 Header 或回退时间，原 Header 在 sample 中 |
| GET `/api/raw/{id}/video/{record或header}` | 按对应真实时钟封装的原始四宫格 MP4，支持 HTTP Range；不可用404 |

清单公开两种时钟 origin_ns（十进制字符串防止 JS 精度损失）及 duration、topic 字段/计数/时序诊断、相机布局和视频时间。原始录制可位于服务账号有权限访问的任意目录，MCAP 清单引用仍必须在对应录制内；每次数据/媒体访问复核源指纹。原始查看不产生转换任务、不改变 check 规则，详见[原始查看器](raw-viewer.md)。

## Doctor 操作

| 路径 | 行为 |
|---|---|
| GET `/api/doctor/options` | 支持的operations/checks/fixes/policies |
| GET `/api/doctor/report?dataset=ID&refresh=false` | 按源指纹缓存的诊断；refresh要求重新计算 |
| POST `/api/doctor/jobs` | 结构化Doctor任务，结果进入共享队列 |

请求公共字段：operation、dataset/datasets、checks、max_episodes、urdf、fail_on、fixes、dry_run、output、confirm、threshold、min_active、keep_static、drop_threshold、policy、chunk_size、post_merge。各默认值和写入规则见使用手册。fix/trim需confirm=true和新output；merge-check需至少两个不同数据集，post_merge=true时只接收一个已合并数据集。

```json
{"operation":"check","dataset":"local/REGISTERED_ID","checks":["metadata","temporal","videos"],"max_episodes":20,"fail_on":"fail"}
```

质量检查/策略gate失败可返回非零worker状态，同时仍有result/report。区分质量结论和执行异常，不将所有failed都当作服务bug。

## Editor

| 路径 | 行为 |
|---|---|
| GET `/api/editor/options` | editor是否可加载、版本、codec presets及操作范围 |
| GET `/api/editor/inspect?dataset=ID&offset=0&limit=100` | 元数据、当前源指纹和episode分页；limit最大200 |
| POST `/api/editor/jobs` | operation=plan/apply，强制计划与参数一致 |

先inspect取得source_fingerprint，用原episode ID配置修改：

```json
{
  "dataset":"REGISTERED_ID",
  "output":"/home/user/datasets/new_edited",
  "operation":"plan",
  "source_fingerprint":"REPLACE_WITH_64_HEX_FROM_INSPECT",
  "task_by_episode":{"0":"Pick up the block"},
  "delete_episodes":[],
  "remove_features":[],
  "video":null,
  "workers":null,
  "codec_threads":null,
  "batch_rows":65536
}
```

示例指纹必须替换为真实64位hex。plan成功后，保留完全相同的source/options/output，operation改为apply并填plan_job_id。改变task、删除项、视频参数或源内容后需重新计划。两个请求不能同时删除episode又改其task。视频子对象包含codec、size、pixel_format、crf、preset、quality、video_keys；支持范围见使用手册。输出格式/FPS继承源，不接受overwrite或任意FFmpeg路径。

## Viewer与Annotation

| 方法 / 路径 | 行为 |
|---|---|
| GET `/api/viewer/status` | 被管理Viewer的ready/error |
| GET `/viewer/local/{id}/0` | Viewer页面，后续episode路由由上游负责 |
| GET/HEAD `/datasets/{org}/{alias}/resolve/main/{path}` | 已登记数据集的受限文件读取与Range支持 |
| GET `/api/health` | Annotation能力，不是平台health |
| POST `/api/dataset/load` | 以repo_id或local_path解析已允许的数据集 |
| GET/POST `/api/episodes/{episode}/atoms` | 读取/保存语言atoms；保存写当前数据集元数据 |
| GET `/api/episodes/{episode}/frame_timestamps` | 读取对应episode时间戳 |
| POST `/api/export` | 将语言atoms物化到一个新输出 |
| POST `/api/push_to_hub` | 显式Hub上传，需要认证及目标；普通转换不调用 |

DatasetRef使用repo_id（如local/ID）或local_path；保存请求还含episode_index和atoms。atom包含role、content、style、timestamp、camera、tool_calls；时间须有限、非负、属于episode范围，camera/style须被支持。服务拒绝畸形已有标注，避免覆盖掉无法读取的内容。

导出参数output_dir可省略，服务会生成新的私有exports目录；copy_videos默认false。Hub请求中的hf_token不要写入仓库、日志或文档示例；平台只支持显式新仓库上传，不把传来的push_in_place=true当成覆盖许可。

## 日志与副作用边界

请求/状态/报告保存在私有`.state/jobs/<id>`；API返回可能包含服务器路径和采集信息，导出报告按内部数据处理。Annotation保存不走队列，不能与对同一源的编辑/裁剪同时进行；源指纹变化会使相关任务失败。任何成功状态都只证明对应输入、参数和验证范围，不保证动作成功率或策略训练效果。
