# HTTP API与任务契约

平台0.2.0；OpenAPI来自运行中的`/openapi.json`，交互schema在`/docs`。以下文档解释行为，字段的最终约束由`models.py`、`doctor.py`、`editor.py`和`viewer.py`的Pydantic模型定义。未声明字段会被拒绝，不能用请求体选择任意Python模块或shell命令。

## 访问与错误

可信网络内无需浏览器登录或Cookie。带Origin的写请求必须同源；无Origin的CLI/API调用仍可使用。这不是身份认证。服务端统一处理常见无效值400、无权限403、路径缺失404；模型错误422、能力不可用503。已接受任务一般返回202和job对象，之后的异步失败从任务状态和报告读取。

示例均在服务器本机调用，不含凭证：

```bash
curl -fsS http://127.0.0.1:8095/health
curl -fsS http://127.0.0.1:8095/api/environment
curl -fsS 'http://127.0.0.1:8095/api/browse?path=/home/user/data_recorded'
```

路径参数应正常URL编码；不要将用户路径直接拼成shell表达式。所有路径是服务器路径，并受允许根目录与输入/输出分离规则限制。

## 工作台、注册与队列

| 方法 / 路径 | 行为 |
|---|---|
| GET `/health` | 平台进程健康，不等于每个可选组件都就绪 |
| GET `/api/environment` | Python/LeTools根路径、provider来源版本、白名单、Viewer状态和发现错误 |
| GET `/api/browse?path=...` | 目录浏览；不是任意文件内容下载 |
| GET `/api/recordings?path=...&provider=tianji` | 当前可选原始录制及不完整标记 |
| POST `/api/jobs` | 提交check或convert |
| GET `/api/jobs`、`/api/jobs/{id}` | 历史或单条状态；报告可能很大，客户端按需取单条 |
| POST `/api/jobs/{id}/cancel` | 取消排队/运行任务；终态重复调用返回现有状态 |
| GET `/api/jobs/{id}/report` | 下载完整任务JSON |
| GET / POST `/api/datasets` | 列出数据集 / 以`{"path":"/absolute/path"}`登记已有数据集 |

原始转换请求：

```json
{
  "kind": "convert",
  "provider": "tianji",
  "source_root": "/home/user/data_recorded/pick_and_place",
  "recordings": ["/home/user/data_recorded/pick_and_place/my_bag-EXAMPLE"],
  "destination": "/home/user/datasets/new_pick_and_place_v21",
  "target_version": "v2.1",
  "params": {"task": "Pick and place", "fps": 50},
  "workers": 2,
  "video_workers": 4
}
```

此处轨迹名是示例，先从recordings接口取得真实路径。check可省略destination；convert必须新目录。所有选中项必须来自当前source_root列表，不可重复。params公开字段和默认值见[使用手册](usage.md#设置参数)；不接受wrench_fps、video_encoder或prepare_valid_only等未暴露字段。

请求没有幂等key：重复POST会创建新job，并可能因输出预留/已存在而被拒绝。提交超时先查任务记录，不要盲目重发。队列一次一个作业，退出码与result决定终态；取消保留日志/中间产物，重启标记interrupted，不自动恢复执行。

典型状态字段：id、kind、status、stage、progress、stages、elapsed_seconds、check_report、check_results、result、error、logs、request。转换最新阶段为check_prepare/convert/validate/publish；历史任务可含独立check/prepare。进度不承诺逐帧百分比。

## Doctor

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
