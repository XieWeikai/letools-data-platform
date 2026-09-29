import { describe, expect, test } from "bun:test";
import { platformProgressCandidates } from "./platform-progress";

const candidates = ["sarm_progress.parquet", "srm_progress.parquet"] as const;

describe("optional local progress files", () => {
  test("absent or untrusted manifest does not issue optional parquet requests", () => {
    for (const available of [undefined, null, [], {}, "sarm_progress.parquet"]) {
      const requests: string[] = [];
      for (const file of platformProgressCandidates("local/example", candidates, available)) {
        requests.push(file);
      }
      expect(requests).toEqual([]);
    }
  });

  test("present files retain upstream candidate priority and chart discovery", () => {
    expect(platformProgressCandidates("local/example", candidates, ["srm_progress.parquet"]))
      .toEqual(["srm_progress.parquet"]);
    expect(platformProgressCandidates("local/example", candidates, [...candidates].reverse()))
      .toEqual([...candidates]);
  });

  test("manifest cannot inject arbitrary paths or duplicate requests", () => {
    expect(platformProgressCandidates("local/example", candidates,
      ["../private", "/etc/passwd", "srm_progress.parquet", "srm_progress.parquet", null]))
      .toEqual(["srm_progress.parquet"]);
  });

  test("remote Hub datasets preserve upstream optional-file discovery", () => {
    expect(platformProgressCandidates("owner/dataset", candidates, undefined)).toBe(candidates);
  });
});
