# 开发与测试

先阅读[架构](architecture.md)和仓库`CLAUDE.md`。生产使用Linux/Python3.12+、FastAPI/Pydantic、原生HTML/CSS/JS；Viewer是固定上游的Next.js/Bun副本。不要为了改Web功能直接修改LeTools/provider源码或上游子模块。

## 开发入口

在单独checkout和状态目录开发，使用[部署指南](deployment.md)准备同一个完整venv。相邻生产服务可以继续使用8095/3010；独立开发端口示例：

```bash
export DWP_PROJECT_ROOT="$PWD"
export DWP_STATE_DIR="$PWD/.state/dev"
export DWP_PYTHON="$HOME/projects/letools-tianji/.venv/bin/python"
export DWP_HOST=127.0.0.1
export DWP_PORT=18096
export DWP_VIEWER_PORT=13011
"$DWP_PYTHON" setup_viewer.py
"$DWP_PYTHON" -m data_web_platform.app
```

独立state意味着独立任务历史、注册表、令牌和Viewer缓存。不要将测试绑定到生产state后以为只改了端口。代码中的服务生命周期可在单元测试里通过`start_services=False`关闭。

## 注释与接口约定

- 模块docstring说明所属边界、数据流与副作用；复杂分支注释解释原因和不变量，不逐行复述语法。
- Pydantic拒绝未知选项、非有限值和不支持的组合；用户提供的路径/文本不能进入shell、任意module或innerHTML。
- 传递Config、Registry、JobManager依赖，避免新的可变全局状态。
- 耗时工作进入worker或线程池；worker只用标准DWP_EVENT协议报告真实状态。未知进度显示阶段与耗时，不造百分比。
- 新输出、源稳定、取消与发布是接口语义。修复异常不能静默关闭检查、修改原始MCAP或覆盖现有目录。
- 测试数据在临时目录生成；真实录制、截图、令牌、venv与Viewer构建只放私有`.state`，不能提交Git。

## Python回归

```bash
cd "$HOME/projects/data-web-platform"
"$HOME/projects/letools-tianji/.venv/bin/python" scripts/check_environment.py --strict
"$HOME/projects/letools-tianji/.venv/bin/python" -m pytest -q
node --check src/data_web_platform/static/app.js
node --check src/data_web_platform/static/editor.js
```

测试覆盖请求/路径、无登录同源保护、选择与输出碰撞、队列恢复/取消、不可重用PID、视频Range/标注、Doctor副本、Editor计划一致性、真实合成视频及融合worker。PyAV/FFmpeg/editor不可用时部分媒体测试会skip；发布验收必须记录skip原因，不能把缺依赖的绿色结果当完整功能证明。

## Viewer适配和前端

```bash
DWP_VIEWER_SOURCE="$PWD/.state/viewer-cache/source" \
  "$HOME/.bun/bin/bun" test viewer_overlay/ tests/viewer-fetch-integration.test.ts
```

这些测试包含实际上游loader契约；缓存需按固定来源构建。`setup_viewer.py`的替换锚点使用精确出现次数，新上游版本不匹配时应停下审查并更新适配，不能改成宽泛regex使构建勉强通过。不要直接编辑生成缓存后把它当成源码修复。

浏览器验收应从无登录Cookie的新上下文开始：选择/清空/多选、真实阶段、结果报告、四路视频播放/seek、分析页、标注保存/回读/导出和Editor计划/执行。截图应来自实际界面；教学标注与隐私处理只改截图副本，不改录制数据。

仓库保留两份Playwright smoke脚本作为可选独立QA工具，需自行安装Playwright及浏览器。它们面向四相机Tianji测试集，不是平台运行依赖。`browser_smoke.cjs`会在专用fixture上暂时修改标注并最终恢复，同时创建新导出；只对明确测试数据运行。配置`DWP_TEST_ORIGIN`、`DWP_TEST_SOURCE`、`DWP_TEST_SSH_HOST`以及需要时的`PLAYWRIGHT_MODULE`/`DWP_TEST_BROWSER`，不要用于未知用户数据。

## 真实数据端到端验收

在服务器上给出明确的正常录制和已知异常录制，所有输出都应位于新测试目录：

```bash
"$HOME/projects/letools-tianji/.venv/bin/python" scripts/smoke_pipeline.py \
  --source /path/to/known-good-recording \
  --negative /path/to/known-bad-recording \
  --task 'Pick and place'
```

没有异常fixture时显式使用`--skip-negative`并记录缺口。结果`summary.json`可交给`scripts/smoke_doctor.py --summary <该文件>`，后者创建独立注册表/任务队列、私有输入快照和全新fix/trim结果，不绑定生产端口。耗时/质量结论必须写明机器、版本、输入集合和验证范围。

## 如何增加一个provider

1. 在独立provider包实现SourceProvider和DatasetSource，安装进配置的venv，验证`letools providers list`。
2. 为其增加受限参数模型、原始录制发现、选择约束和worker适配。平台不会自动把任意Python参数当表单或执行代码。
3. 明确检查失败、cache、源变化、进度、取消、目标发布的语义；不要复用Tianji阈值假装适配另一台机器人。
4. 补充真实/合成输入的成功和失败测试，更新功能范围、API和使用文档。

目标backend、核心模型、planner/native优化应在LeTools项目评审；本项目只负责调用契约。Provider和输出backend也不是Doctor、Editor或Viewer的别名。

## 提交和发布

按独立行为分commit，提交代码、相应测试与解释该行为的文档。历史测量单独注明日期，不重写成当前承诺；`deploy/sources.json`和依赖约束应随验收更新。发布前检查`git diff --check`、未跟踪文件与凭证/数据目录，验证从干净venv安装、服务启动及一条真实任务，再推送。只有确认队列空闲时才能部署和重启自己的用户服务。
