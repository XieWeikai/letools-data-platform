"""Execute upstream editor in a queued process, validating before publication."""
from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import asdict
from pathlib import Path

from .datasets import dataset_fingerprint
from .doctor_worker import emit, stage
from .editor import EditorRequest, edit_config, safe_source
from .paths import validate_output_path
from .publishing import new_staging, publish_noreplace


def run_prepared(request, workspace):
    """Run a queued plan or apply using only validated paths and typed options.

    Plan is read-only and stops before staging. Apply writes an exclusive
    sibling staging directory, validates the resulting dataset, rechecks the
    source and final path, then publishes without replacement. Failed staging
    remains available for diagnosis; the original dataset is never overwritten.
    """
    from letools_editor import plan_edit, edit_dataset
    from letools.validation import validate_dataset
    timings = {}
    workspace = Path(workspace)
    with stage('preflight', timings):
        if request.get('kind') != 'editor':
            raise ValueError('无效的 editor 任务')
        options = EditorRequest.model_validate(request['options'])
        roots = request['allowed_roots']
        source, info = safe_source(request['source'], roots)
        output = validate_output_path(options.output, roots, [source])
        before = dataset_fingerprint(source)
        if before != options.source_fingerprint:
            raise ValueError('排队期间源数据发生变化，请重新加载并预览')
        config = edit_config(options)
    with stage('editor_plan', timings):
        plan = asdict(plan_edit(source, output, config))
        (workspace / 'editor-plan.json').write_text(json.dumps(plan, indent=2, default=str, ensure_ascii=False))
        if dataset_fingerprint(source) != before:
            raise ValueError('计划期间源数据发生变化')
    if options.operation == 'plan':
        emit('result', result={'operation': 'plan', 'preview_only': True, 'plan': plan, 'timings': timings,
                               'source_unchanged': True})
        return 0
    staging = new_staging(output)
    emit('log', message=f'编辑输出暂存于 {staging}；完整验证后发布到 {output}')
    with stage('editor_apply', timings):
        result = asdict(edit_dataset(source, staging, config))
    with stage('validate', timings):
        _, edited_info = safe_source(staging, roots)
        if edited_info['codebase_version'] != info['codebase_version']:
            raise ValueError('编辑结果版本发生变化')
        validation = asdict(validate_dataset(staging, deep=True))
        emit('validation', report=validation)
        if not validation['valid']:
            raise ValueError('编辑输出验证失败，未发布目标目录')
        if dataset_fingerprint(source) != before:
            raise ValueError('编辑期间源数据发生变化，未发布输出')
    with stage('publish', timings):
        # Revalidate canonical output and forbid a newly substituted parent symlink.
        if validate_output_path(options.output, roots, [source]) != output:
            raise ValueError('输出路径在任务期间发生变化')
        publish_noreplace(staging, output)
    validation['path'] = str(output)
    emit('result', result={'operation': 'apply', 'destination': str(output), 'plan': plan,
                           'editor_result': result, 'validation': validation, 'timings': timings,
                           'episodes': edited_info['total_episodes'], 'frames': edited_info['total_frames'],
                           'source_unchanged': True})
    return 0


def main():
    try:
        os.nice(10)
        path = Path(sys.argv[1])
        return run_prepared(json.loads(path.read_text()), path.parent)
    except Exception as error:
        emit('error', message=str(error))
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
