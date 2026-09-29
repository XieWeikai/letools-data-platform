/** Browser URLs stay relative and use the platform session cookie.  SSR alone
 * knows the token-protected loopback data bridge.  No NEXT_PUBLIC secret. */
export function platformFetchUrl(url: string): string {
  if (typeof window === "undefined" && url.startsWith("/datasets/")) {
    const origin = process.env.DWP_DATA_ORIGIN;
    if (!origin || !/^http:\/\/127\.0\.0\.1:\d+$/.test(origin)) {
      throw new Error("Platform data bridge is unavailable");
    }
    return origin + url;
  }
  return url;
}

export function platformRequestInit(url: string, init: RequestInit = {}): RequestInit {
  if (typeof window === "undefined" && url.startsWith("/datasets/")) {
    const token = process.env.DWP_INTERNAL_TOKEN;
    if (!token) throw new Error("Platform data bridge is not authenticated");
    const headers = new Headers(init.headers);
    headers.set("X-DWP-Internal-Token", token);
    return { ...init, headers };
  }
  return init;
}

export function platformFetch(url: string, init: RequestInit = {}): Promise<Response> {
  return fetch(platformFetchUrl(url), platformRequestInit(url, init));
}
