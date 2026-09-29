/** Local metadata is enumerated by the authenticated dataset bridge.
 * null means a Hub dataset: retain the upstream discovery implementation.
 * An absent/invalid local manifest must never fall back to probing paths.
 */
export function platformEpisodeMetadataPaths(
  repoId: string,
  available: unknown,
): string[] | null {
  if (!repoId.startsWith("local/")) return null;
  if (!Array.isArray(available)) return [];
  const pattern = /^meta\/episodes\/chunk-[0-9]+\/file-[0-9]+\.parquet$/;
  return [...new Set(available.filter((value): value is string =>
    typeof value === "string" && value.match(pattern)?.[0] === value))];
}
