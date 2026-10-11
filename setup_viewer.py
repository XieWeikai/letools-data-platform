#!/usr/bin/env python3
"""Prepare/build the complete pinned viewer in the platform's own cache.

Run with the isolated LeTools Python: python setup_viewer.py [--no-install]
The original LeTools source and its user-wide visualizer cache are read-only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from data_web_platform.config import Config


@contextmanager
def private_git_context(state_dir: Path):
    """Keep git apply inside the generated cache, even below a Git checkout.

    Git otherwise discovers the platform's parent repository and can silently
    skip every patch path while returning success. Setup is a standalone CLI,
    not a threaded server operation; restore the caller's Git environment even
    when preparing the cache fails. Neither upstream checkout is modified.
    """
    names = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
             "GIT_PREFIX", "GIT_CEILING_DIRECTORIES")
    previous = {name: os.environ.pop(name, None) for name in names}
    os.environ["GIT_CEILING_DIRECTORIES"] = str(state_dir.resolve())
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def prepare_upstream(config):
    """Verify the Doctor hook, repairing older falsely marked private caches."""
    from letools.visualizer import prepare_visualizer

    def patched(installation):
        file = installation.source / "src/app/[org]/[dataset]/[episode]/episode-viewer.tsx"
        source = file.read_text()
        return all(anchor in source for anchor in (
            "process.env.NEXT_PUBLIC_LEROBOT_DOCTOR_URL", "href={doctorUrl}", "src={doctorUrl}"))

    with private_git_context(config.state_dir):
        options = {"cache_dir": config.state_dir / "viewer-upstream", "install_dependencies": False}
        upstream = prepare_visualizer(**options)
        if not patched(upstream):
            upstream = prepare_visualizer(**options, force=True)
        if not patched(upstream):
            raise RuntimeError("Pinned viewer is missing the configurable local Doctor integration")
        return upstream


def _replace(path: Path, old: str, new: str, count: int = 1):
    source = path.read_text()
    actual = source.count(old)
    if actual != count:
        raise RuntimeError(f"Pinned viewer changed: {path.name}: expected {count} adapter anchors, found {actual}")
    path.write_text(source.replace(old, new))


def apply_overlay(source: Path, overlay: Path):
    """Fail closed when an upstream anchor changes; never broadly regex patch."""
    _replace(source / "next.config.ts", "const nextConfig: NextConfig = {", 'const nextConfig: NextConfig = {\n  basePath: "/viewer",')
    shutil.copyfile(overlay / "platform-fetch.ts", source / "src/utils/platform-fetch.ts")
    shutil.copyfile(overlay / "platform-progress.ts", source / "src/utils/platform-progress.ts")
    shutil.copyfile(overlay / "platform-actions.ts", source / "src/utils/platform-actions.ts")
    shutil.copyfile(overlay / "platform-episodes.ts", source / "src/utils/platform-episodes.ts")
    version = source / "src/utils/versionUtils.ts"
    _replace(version, "export interface DatasetInfo {", "export interface DatasetInfo {\n  _dwp_optional_files?: string[];\n  _dwp_episode_shards?: string[];")
    _replace(version, 'import { authHeaders } from "./auth";', 'import { authHeaders } from "./auth";\nimport { platformFetch } from "./platform-fetch";')
    _replace(version, 'const DATASET_URL =\n  process.env.DATASET_URL || "https://huggingface.co/datasets";',
             'function datasetBase(repoId: string): string {\n  return repoId.startsWith("local/") ? "/datasets" : "https://huggingface.co/datasets";\n}')
    _replace(version, "${DATASET_URL}/${repoId}", "${datasetBase(repoId)}/${repoId}", 3)
    _replace(version, "await fetch(", "await platformFetch(", 2)
    parquet = source / "src/utils/parquetUtils.ts"
    _replace(parquet, 'import { authHeaders } from "./auth";', 'import { authHeaders } from "./auth";\nimport { platformFetch, platformFetchUrl, platformRequestInit } from "./platform-fetch";')
    _replace(parquet, "await fetch(url,", "await platformFetch(url,")
    _replace(parquet, '    url,\n    requestInit: { cache: "no-store", headers: authHeaders() },',
             '    url: platformFetchUrl(url),\n    requestInit: platformRequestInit(url, { cache: "no-store", headers: authHeaders() }),')
    data = source / "src/app/[org]/[dataset]/[episode]/fetch-data.ts"
    text = data.read_text()
    data.write_text('import { platformFetch } from "@/utils/platform-fetch";\n'
                    'import { platformProgressCandidates } from "@/utils/platform-progress";\n'
                    'import { platformActionFeature } from "@/utils/platform-actions";\n'
                    'import { platformEpisodeMetadataPaths } from "@/utils/platform-episodes";\n' + text)
    _replace(data, "await fetch(tasksUrl,", "await platformFetch(tasksUrl,")
    _replace(data, "  episodeId: number,\n): Promise<((episodeDuration: number) => ChartRow[]) | null> {",
             "  episodeId: number,\n  availableProgressFiles?: string[],\n): Promise<((episodeDuration: number) => ChartRow[]) | null> {")
    _replace(data, "for (const progressPath of PROGRESS_PARQUET_CANDIDATES)",
             "for (const progressPath of platformProgressCandidates(repoId, PROGRESS_PARQUET_CANDIDATES, availableProgressFiles))")
    _replace(data, "loadEpisodeProgressGroup(repoId, version, episodeId),",
             "loadEpisodeProgressGroup(repoId, version, episodeId, rawInfo._dwp_optional_files),")
    # Local v3 discovery must not infer nonexistent next shard names. The Hub
    # paths retain their original probing behavior and need no platform API.
    _replace(data, "  getDatasetVersionAndInfo,", "  getDatasetVersionAndInfo,\n  getDatasetInfo,")
    _replace(data, "): AsyncGenerator<Record<string, unknown>[], void, unknown> {\n  let chunkIndex = 0;",
             "): AsyncGenerator<Record<string, unknown>[], void, unknown> {\n"
             '  if (repoId.startsWith("local/")) {\n'
             "    const info = await getDatasetInfo(repoId);\n"
             "    const paths = platformEpisodeMetadataPaths(repoId, info._dwp_episode_shards) ?? [];\n"
             "    for (const path of paths) {\n"
             "      const buf = await fetchParquetFile(buildVersionedUrl(repoId, version, path));\n"
             "      const rows = await readParquetAsObjects(buf, []);\n"
             "      if (rows.length > 0) yield rows;\n"
             "    }\n"
             "    return;\n"
             "  }\n"
             "  let chunkIndex = 0;")
    _replace(data, "    const allEpisodes: { index: number; length: number }[] = [];\n    let fileIndex = 0;",
             "    const allEpisodes: { index: number; length: number }[] = [];\n"
             '    if (repoId.startsWith("local/")) {\n'
             "      for await (const rows of iterateEpisodeMetadataFilesV3(repoId, version)) {\n"
             "        for (const row of rows) {\n"
             "          const parsed = parseEpisodeRowSimple(row);\n"
             "          allEpisodes.push({ index: parsed.episode_index, length: parsed.length });\n"
             "        }\n"
             "      }\n"
             "    } else {\n"
             "    let fileIndex = 0;")
    _replace(data, "\n    if (allEpisodes.length === 0) return null;",
             "\n    }\n\n    if (allEpisodes.length === 0) return null;")
    _replace(data, '  const actionEntry = Object.entries(info.features).find(\n'
             '    ([key, f]) => key === "action" && f.shape.length === 1,\n  );',
             '  const actionEntry = platformActionFeature(info.features);')
    action_panel = source / "src/components/action-insights-panel.tsx"
    _replace(action_panel, 'import { CHART_CONFIG } from "@/utils/constants";',
             'import { CHART_CONFIG } from "@/utils/constants";\n'
             'import { platformIsActionColumn } from "@/utils/platform-actions";')
    _replace(action_panel, 'k.startsWith("action")',
             'platformIsActionColumn(k, CHART_CONFIG.SERIES_NAME_DELIMITER)', 2)
    annotations = source / "src/utils/annotationsClient.ts"
    _replace(annotations, "function buildUrl(path:", 'function backendOrigin(): string {\n  return typeof window === "undefined" ? "http://127.0.0.1" : window.location.origin;\n}\n\nfunction buildUrl(path:')
    _replace(annotations, ", ENV_URL)", ", backendOrigin())", 6)
    _replace(annotations, "  return ENV_URL;", "  return ENV_URL ? backendOrigin() : null;")
    auth_context = source / "src/context/auth-context.tsx"
    _replace(auth_context, 'fetch("/api/auth/', 'fetch("/viewer/api/auth/', 3)
    _replace(source / "src/utils/auth.ts", "return `/api/proxy", "return `/viewer/api/proxy")
    # Avoid a build-time network dependency on Google's font service.  This is
    # typography only; all application components/routes remain upstream.
    layout = source / "src/app/layout.tsx"
    _replace(layout, 'import { Inter } from "next/font/google";\n', "")
    _replace(layout, 'const inter = Inter({ subsets: ["latin"] });\n', "")
    _replace(layout, "<body className={inter.className}>", '<body style={{ fontFamily: "Inter, system-ui, sans-serif" }}>')


def prepare(config, *, install: bool = True, build: bool = True, force: bool = False) -> Path:
    upstream = prepare_upstream(config)
    overlay = config.project_root / "viewer_overlay"
    digest = hashlib.sha256(upstream.fingerprint.encode())
    digest.update(Path(__file__).read_bytes())
    for path in sorted(overlay.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(overlay).as_posix().encode())
            digest.update(path.read_bytes())
    identity = digest.hexdigest()
    cache = config.state_dir / "viewer-cache"
    source = cache / "source"
    marker = cache / "platform-overlay.json"
    cache.mkdir(parents=True, exist_ok=True)
    previous = json.loads(marker.read_text()) if marker.is_file() else {}
    if force or previous.get("fingerprint") != identity or not source.is_dir():
        stage = Path(tempfile.mkdtemp(prefix=".overlay-", dir=cache))
        stale = None
        try:
            shutil.copytree(upstream.source, stage, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("node_modules", ".next", ".git"))
            apply_overlay(stage, overlay)
            if source.exists():
                stale = cache / (".old-source-" + identity[:12])
                if stale.exists():
                    raise RuntimeError(f"Stale cache exists: {stale}; inspect before rebuilding")
                os.rename(source, stale)
            os.rename(stage, source)
            marker.write_text(json.dumps({"fingerprint": identity, "upstream_commit": upstream.upstream_commit}, indent=2))
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            if stale and stale.exists() and not source.exists():
                os.rename(stale, source)
            raise
        if stale:
            shutil.rmtree(stale)
    environment = config.environment()
    environment.update({"NEXT_TELEMETRY_DISABLED": "1", "NEXT_PUBLIC_ANNOTATE_BACKEND_URL": "/",
                        "NEXT_PUBLIC_LEROBOT_DOCTOR_URL": "/doctor"})
    # Build must not bake an internal token or a machine-specific bridge URL
    # into client assets.  Runtime process.env accesses supply these for SSR.
    environment.pop("DWP_INTERNAL_TOKEN", None)
    environment.pop("DWP_DATA_ORIGIN", None)
    if install:
        subprocess.run([str(config.bun), "install", "--frozen-lockfile"], cwd=source, env=environment, check=True)
    if build:
        if not (source / "node_modules").is_dir():
            raise RuntimeError("Install viewer dependencies before building")
        subprocess.run([str(config.bun), "run", "build"], cwd=source, env=environment, check=True)
    return source


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-install", action="store_true")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    config = Config.from_env()
    config.initialize()
    print(prepare(config, install=not args.no_install, build=not args.no_build, force=args.force))


if __name__ == "__main__":
    main()
