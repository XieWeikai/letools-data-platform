/** Recognize the existing Tianji action vector without renaming any column. */
export function platformIsActionColumn(key: string, delimiter = " | "): boolean {
  return key.startsWith("action") || key === "observation.action" ||
    key.startsWith(`observation.action${delimiter}`);
}

/** Keep upstream's canonical action preference when both schemas coexist. */
export function platformActionFeature<T extends { shape: number[] }>(
  features: Record<string, T>,
): [string, T] | undefined {
  for (const name of ["action", "observation.action"]) {
    const feature = features[name];
    if (feature?.shape.length === 1) return [name, feature];
  }
  return undefined;
}
