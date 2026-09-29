import { describe, expect, test } from "bun:test";
import { platformEpisodeMetadataPaths } from "./platform-episodes";

describe("local v3 metadata manifest", () => {
  test("only requests actual shards including sparse files and later chunks", () => {
    const manifest = ["meta/episodes/chunk-000/file-000.parquet",
      "meta/episodes/chunk-000/file-007.parquet", "meta/episodes/chunk-004/file-009.parquet"];
    expect(platformEpisodeMetadataPaths("local/example", manifest)).toEqual(manifest);
  });

  test("absent or invalid manifests never enable trial-and-404 discovery", () => {
    for (const manifest of [undefined, null, {}, "meta/episodes/chunk-000/file-000.parquet", []]) {
      expect(platformEpisodeMetadataPaths("local/example", manifest)).toEqual([]);
    }
  });

  test("rejects arbitrary paths, nested traversal, URLs, nonstrings and duplicates", () => {
    const path = "meta/episodes/chunk-000/file-000.parquet";
    expect(platformEpisodeMetadataPaths("local/example", [path, path, null, 1,
      "../secret", "/meta/episodes/chunk-000/file-000.parquet", "https://host/private",
      "meta/episodes/chunk-000/../info.json", "meta/episodes/chunk-000/file-000.parquet?x=1",
      "meta/episodes/chunk-000/file-000.parquet\n", "data/chunk-000/file-000.parquet",
      "meta/episodes/chunk-000\\file-000.parquet"])).toEqual([path]);
  });

  test("Hub discovery remains upstream even when a repository supplies a manifest", () => {
    expect(platformEpisodeMetadataPaths("owner/dataset", [])).toBeNull();
    expect(platformEpisodeMetadataPaths("locality/dataset", undefined)).toBeNull();
  });
});
