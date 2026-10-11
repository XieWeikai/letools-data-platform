"""Read-only deployment preflight for the documented shared, private venv.

This script never installs packages, probes a GPU, creates credentials, builds
the Viewer or starts services. A successful result proves dependencies and CLI
entry points are available; live conversion and browser checks remain separate.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

from data_web_platform.config import Config


def command(argv, *, env=None):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
        return {'ok': result.returncode == 0, 'returncode': result.returncode,
                'output': (result.stdout or result.stderr).strip()[-4000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'ok': False, 'error': str(error)}


def inspect():
    config = Config.from_env()
    errors = []
    in_venv = sys.prefix != sys.base_prefix
    if not in_venv:
        errors.append('请使用专用 venv 的 Python')
    # Comparing the executable's spelling preserves distinct venvs whose Python
    # symlinks all resolve to the same system binary.
    if Path(sys.executable).parent != config.python.parent:
        errors.append('当前 API Python 与配置的 worker Python 不在同一 bin 目录')
    packages = {}
    for name in ('data-web-platform', 'letools', 'letools-native', 'tianji-provider',
                 'letools-editor', 'av', 'opencv-python-headless', 'imageio-ffmpeg'):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
            errors.append(f'缺少包: {name}')
    modules = {}
    for name in ('data_web_platform', 'letools', 'tianji_provider', 'letools_editor', 'letools_editor._native'):
        try:
            modules[name] = getattr(importlib.import_module(name), '__file__', None)
        except (ImportError, OSError) as error:
            errors.append(f'无法加载 {name}: {error}')
    environment = config.environment()
    cli = command([str(config.python), '-m', 'letools.cli', 'providers', 'list'], env=environment)
    providers = []
    if cli['ok']:
        try:
            providers = json.loads(cli['output'])
            if not any(item['name'] == 'tianji' for item in providers):
                errors.append('worker 环境未发现 tianji entry point')
        except (ValueError, TypeError, KeyError):
            errors.append('providers list 未返回有效注册表')
    else:
        errors.append('worker 环境不能执行 letools providers list')
    tools = {'bun': command([str(config.bun), '--version']),
             'ffmpeg': command(['/usr/bin/ffmpeg', '-version'])}
    for name, result in tools.items():
        if not result['ok']:
            errors.append(f'运行工具不可用: {name}')
    tools['ffmpeg']['output'] = tools['ffmpeg'].get('output', '').split('\n')[0]
    return {'ok': not errors, 'python': sys.executable, 'venv': in_venv,
            'worker_python': str(config.python), 'project_root': str(config.project_root),
            'letools_root': str(config.letools_root), 'state_dir': str(config.state_dir),
            'packages': packages, 'modules': modules, 'providers': providers,
            'tools': tools, 'errors': errors,
            'note': 'GPU capability is tested by provider auto at conversion time; this is not a live media/build acceptance.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--strict', action='store_true', help='Return nonzero for a missing full-platform prerequisite')
    args = parser.parse_args()
    report = inspect()
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 1 if args.strict and not report['ok'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
