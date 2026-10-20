# 2026-10-20：统一服务账号文件系统访问

范围：Data Web Platform 0.5.3；不修改 LeTools/provider、8081数采服务、挂载配置或原始/已转换数据。

## 故障与修复

旧 `/api/environment` 对所有允许根目录调用 `is_dir()`。旧配置中的
`/media/user/BAG_STORAGE` 是自动挂载点，其对应设备已不在线，触发
`OSError: [Errno 19] No such device`，环境加载因此返回500，页面无法完成provider加载。

新服务不接受全局目录白名单，`DWP_ALLOWED_ROOTS` 被忽略。目录浏览/补全/新建、
转换、原始查看、登记、Doctor、Editor、Viewer/标注导出统一按服务账号OS权限处理。
home和`/`是导航快捷入口而非限制。环境接口不访问这些快捷入口的文件系统状态。
用户输入仍规范化为绝对路径；内部数据集/MCAP引用约束、新输出、防覆盖、输入输出
分离、源数据指纹检查、同源写入检查不变。队列兼容旧的显式范围请求。

## 隔离验收

- 在Marvin专用`.venv`内运行候选源码完整测试：**275 passed**（11.43秒），
  包括11个新增用例。仅有现有Starlette/httpx兼容弃用警告。
- 本机另用旧全局LeTools解释器做小范围预检时，缺少`letools_editor`且provider较旧，
  对应两项环境不匹配；没有修改该全局环境。正式验收以Marvin专用环境为准。
- 临时HTTP服务仅监听`127.0.0.1:8097`，使用独立状态目录，验收后已停止。

| HTTP请求 | 状态 | 耗时 | 核验结果 |
|---|---|---|---|
| `/api/environment` | 200 | 0.006s | `service_user=user`、`os_permissions`、四个provider含`tianji` |
| 浏览`/etc` | 200 | 0.044s | 旧白名单之外的可读目录可以直接浏览 |
| 浏览`/root` | 403 | 0.002s | OS权限不足，未提权或绕过 |
| 补全`/nas/tianji/raw_data/peg` | 200 | 3.043s | 匹配`peg_in_hole` |
| 加载`/nas/tianji/raw_data/peg_in_hole` | 200 | 15.818s | 104条原始录制，未转换/写入源数据 |
| 浏览失效`/media/user/BAG_STORAGE` | 503 | 10.039s | 明确提示设备不可用；未再次导致环境接口失败 |

NAS时延与失效自动挂载的超时仍由实际网络/内核挂载状态决定；本次未擅自更改它们。
部署前检查49条任务记录，无queued/running任务、无活动原始预览；保留所有历史。
