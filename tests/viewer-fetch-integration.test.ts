/** Exercise the patched upstream loaders, not a reimplementation of them.
 * Run after setup with DWP_VIEWER_SOURCE pointing at the private candidate.
 * Only in-memory parquet/metadata reads are mocked; no dataset is written.
 */
import { beforeEach, describe, expect, mock, test } from "bun:test";
import { resolve } from "node:path";

const source = process.env.DWP_VIEWER_SOURCE;
if (!source) {
  test.skip("set DWP_VIEWER_SOURCE to exercise patched upstream loaders", () => {});
} else {
  const parquetPath = resolve(source, "src/utils/parquetUtils.ts");
  const versionPath = resolve(source, "src/utils/versionUtils.ts");
  const parquet = await import(parquetPath);
  const version = await import(versionPath);
  let manifest: unknown = [];
  let infoReads = 0;
  let requests: string[] = [];
  let columnsRead: string[][] = [];
  let tables: Record<string, Record<string, unknown>[]> = {};
  mock.module(parquetPath, () => ({
    ...parquet,
    fetchParquetFile: async (url: string) => {
      const path = url.split("/resolve/main/")[1];
      requests.push(path);
      if (!(path in tables)) throw new Error(`Unexpected parquet request: ${path}`);
      return path;
    },
    readParquetAsObjects: async (path: string, columns: string[]) => {
      columnsRead.push(columns);
      return tables[path];
    },
  }));
  mock.module(versionPath, () => ({
    ...version,
    getDatasetInfo: async () => {
      infoReads++;
      return { _dwp_episode_shards: manifest };
    },
  }));
  const loaders = await import(resolve(source, "src/app/[org]/[dataset]/[episode]/fetch-data.ts"));

  const metadata = (episode: number, length: number, file = 0) => ({
    episode_index: episode, length, "data/chunk_index": 0, "data/file_index": file,
    dataset_from_index: episode * 20, dataset_to_index: episode * 20 + length,
  });
  const features = {
    "observation.action": { dtype: "float32", shape: [2], names: ["left", "right"] },
    "observation.state": { dtype: "float32", shape: [2], names: ["left", "right"] },
  };
  const datasetInfo = (version = "v2.1", actionFeatures = features) => ({
    codebase_version: version, total_episodes: 2, fps: 50, chunks_size: 1000,
    data_path: "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
    features: actionFeatures,
  });
  const actionRows = (episode: number) => Array.from({ length: 20 }, (_, frame) => ({
    index: episode * 20 + frame,
    "observation.action": [Math.sin(frame / 3) + episode, frame * 0.01],
    "observation.state": [Math.sin(frame / 3) + episode, frame * 0.01],
    action: [Math.cos(frame / 3), frame * 0.02],
  }));

  describe("private overlay: actual upstream statistics/action loaders", () => {
    beforeEach(() => {
      manifest = [];
      infoReads = 0;
      requests = [];
      columnsRead = [];
      tables = {};
    });

    test("v3 statistics read all and only manifest shards, including gaps and later chunks", async () => {
      manifest = ["meta/episodes/chunk-000/file-000.parquet",
        "meta/episodes/chunk-000/file-007.parquet", "meta/episodes/chunk-004/file-009.parquet"];
      (manifest as string[]).forEach((path, index) => {
        tables[path] = [metadata(index, (index + 1) * 50)];
      });
      const result = await loaders.loadAllEpisodeLengthsV3("local/test", "v3.0", 50);
      expect(result.allEpisodeLengths.map((ep: { lengthSeconds: number }) => ep.lengthSeconds)).toEqual([1, 2, 3]);
      expect(requests).toEqual(manifest);
      expect(infoReads).toBe(1);
    });

    test("local invalid manifests never trigger guessed requests or fake parquet data", async () => {
      manifest = ["../../secret", "meta/episodes/chunk-000/../info.json", "/etc/passwd"];
      expect(await loaders.loadAllEpisodeLengthsV3("local/test", "v3.0", 50)).toBeNull();
      expect(requests).toEqual([]);
    });

    test("Hub statistics retain their existing discovery implementation", async () => {
      tables["meta/episodes/chunk-000/file-000.parquet"] = [metadata(0, 50)];
      const result = await loaders.loadAllEpisodeLengthsV3("owner/test", "v3.0", 50);
      expect(result.allEpisodeLengths.length).toBe(1);
      expect(infoReads).toBe(0);
      expect(requests).toEqual(["meta/episodes/chunk-000/file-000.parquet", "meta/episodes/chunk-000/file-001.parquet"]);
    });

    test("v2 cross-episode statistics consume original observation.action vectors", async () => {
      for (let episode = 0; episode < 2; episode++) {
        tables[`data/chunk-000/episode_${String(episode).padStart(6, "0")}.parquet`] = actionRows(episode);
      }
      const result = await loaders.loadCrossEpisodeActionVariance("local/test", "v2.1", datasetInfo(), 50);
      expect(result.numEpisodes).toBe(2);
      expect(result.actionNames).toEqual(["observation.action | left", "observation.action | right"]);
      expect(columnsRead.every((columns) => columns.includes("observation.action"))).toBe(true);
      expect(result.aggVelocity.length).toBeGreaterThan(0);
    });

    test("v3 cross-episode statistics use manifest shards and original action fields", async () => {
      manifest = ["meta/episodes/chunk-000/file-000.parquet", "meta/episodes/chunk-009/file-007.parquet"];
      for (let episode = 0; episode < 2; episode++) {
        tables[(manifest as string[])[episode]] = [metadata(episode, 20, episode)];
        tables[`data/chunk-000/file-${String(episode).padStart(3, "0")}.parquet`] = actionRows(episode);
      }
      const result = await loaders.loadCrossEpisodeActionVariance("local/test", "v3.0", datasetInfo("v3.0"), 50);
      expect(result.numEpisodes).toBe(2);
      expect(result.actionNames[0]).toBe("observation.action | left");
      expect(requests.filter((path) => path.startsWith("meta/"))).toEqual(manifest);
      expect(requests.length).toBe(4);
    });

    test("canonical action is never overwritten by the Tianji fallback", async () => {
      for (let episode = 0; episode < 2; episode++) {
        tables[`data/chunk-000/episode_${String(episode).padStart(6, "0")}.parquet`] = actionRows(episode);
      }
      const canonicalFeatures = { ...features, action: { dtype: "float32", shape: [2], names: ["canonical_left", "canonical_right"] } };
      const result = await loaders.loadCrossEpisodeActionVariance("local/test", "v2.1", datasetInfo("v2.1", canonicalFeatures), 50);
      expect(result.actionNames).toEqual(["action | canonical_left", "action | canonical_right"]);
      expect(columnsRead.every((columns) => columns.includes("action") && !columns.includes("observation.action"))).toBe(true);
    });
  });
}
