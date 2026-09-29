import { describe, expect, test } from "bun:test";
import { platformActionFeature, platformIsActionColumn } from "./platform-actions";

describe("Tianji action compatibility", () => {
  test("recognizes the existing action column and all flattened dimensions", () => {
    for (const name of ["action", "action | 0", "action.left | joint_1",
      "observation.action", "observation.action | left_joint_1",
      "observation.action | right_gripper"]) {
      expect(platformIsActionColumn(name)).toBe(true);
    }
  });

  test("does not classify observations or similarly named features as actions", () => {
    for (const name of ["timestamp", "observation.state | joint_1",
      "observation.wrench | x", "observation.action_mask",
      "observation.action_labels | label", "observation.action.state"]) {
      expect(platformIsActionColumn(name)).toBe(false);
    }
  });

  test("cross-episode statistics select observation.action without mutating metadata", () => {
    const features = { "observation.action": { shape: [16], names: ["left_joint_1"] },
      "observation.state": { shape: [16], names: ["left_joint_1"] } };
    const before = JSON.stringify(features);
    const selected = platformActionFeature(features);
    expect(selected).toEqual(["observation.action", features["observation.action"]]);
    expect(selected?.[1]).toBe(features["observation.action"]);
    expect(JSON.stringify(features)).toBe(before);
  });

  test("upstream action keeps precedence, regardless of feature insertion order", () => {
    const features = { "observation.action": { shape: [16] }, action: { shape: [7] } };
    expect(platformActionFeature(features)).toEqual(["action", features.action]);
  });

  test("non-vector features are not mistaken for action vectors", () => {
    expect(platformActionFeature({ action: { shape: [3, 2, 2] } })).toBeUndefined();
    const fallback = { action: { shape: [3, 2] }, "observation.action": { shape: [16] } };
    expect(platformActionFeature(fallback)?.[0]).toBe("observation.action");
  });
});
