# 维护入口

Python 3.12 / FastAPI 的单进程工作台，原生 HTML/CSS/JS 操作面板，Next.js/Bun 上游 Viewer 子进程。没有外部数据库；注册表、任务状态与报告以私有 JSON 文件持久化，写入采用临时文件替换和必要的文件锁。

## 请求链路

浏览器 → Cookie 与同源校验 → Pydantic 请求模型 → canonical path / provider / 新目标校验 → 单任务队列 → 独立 Python 子进程 → Tianji check/prepare → LeTools convert → deep validation → 防覆盖发布 → 注册数据集。

读取视频时不重新扫描全部 Parquet；登记数据集和 Doctor 前执行完整布局检查。实际文件路径再次验证，禁止越出数据集或允许根目录。路径策略不是针对同一 Unix 账号恶意并发修改符号链接的操作系统沙箱。

Viewer 由平台管理，监听 loopback。浏览器经 `/viewer` 代理，文件与 Annotation 走同源接口。服务端 Next.js 请求通过独立、带令牌的 loopback 数据桥访问本地数据；内部令牌不会成为公开接口的鉴权旁路。

## 模块

| 要修改的功能 | 文件 |
| --- | --- |
| 路由、登录、provider 发现、录制列表 | `src/data_web_platform/app.py` |
| 环境与隔离配置 | `config.py`、`scripts/install_isolation.py` |
| 参数与拒绝不支持的选项 | `models.py` |
| 文件路径边界、全新输出检查 | `paths.py` |
| 任务持久化、排队、取消与进程清理 | `jobs.py` |
| check → prepare → convert 生命周期 | `worker.py`、`publishing.py` |
| 注册数据集、布局检查与指纹 | `datasets.py` |
| 完整 Viewer 代理、标注、导出 | `viewer.py`、`setup_viewer.py`、`viewer_overlay/` |
| Doctor 参数/报告/网页/副本操作 | `doctor.py`、`doctor_worker.py`、`doctor_compat.py`、`trim.py` |
| 转换工作台界面 | `static/` |
| 单元、真实视频与路由回归 | `tests/test_*.py` |
| 真实服务器端到端验证 | `scripts/smoke_*.py`、`scripts/browser*_smoke.cjs` |

表中 Python 模块与 static 目录均位于 `src/data_web_platform/`。

新增 provider 需先安装到所选 LeTools venv，确认 CLI 发现；再实现对应 Web 参数模型、原始记录选择和 worker 适配。不要让用户输入任意模块名、shell 命令或 Python 代码。未实现 Web 适配的 entry point 仅展示，不能提交任务。

## 约定

- Python snake_case；结构化边界用 Pydantic，业务错误保留可读原因。
- 子进程通过 `DWP_EVENT` JSON 行发送真实阶段/报告；未知进度显示阶段而不是猜百分比。
- 原始录制只读；转换、fix、trim、export 不覆盖既有目标。Annotation 保存是明确的元数据写操作。
- 单元测试用 pytest / FastAPI TestClient；真实转换和浏览器测试另跑，避免常规测试依赖机器人或网络。
- 仅更新自己的服务；不修改 LeTools 源码和子模块来解决 Web 适配问题。
- 本仓库是新建项目，尚无历史 Git 提交约定；初始提交使用 `feat:` 描述交付功能。

构建、运行与排障命令见 README。前端不依赖外部字体/CDN；Viewer 构建固定于 LeTools 所绑定的上游版本。
