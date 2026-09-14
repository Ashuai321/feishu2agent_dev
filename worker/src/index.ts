export interface Env {
  /** Public origin of the Python Feishu2Agents service, without a trailing slash. */
  PYTHON_ORIGIN: string;
}

const FEISHU_WEBHOOK_PUBLIC_PATH = "/feishu/events";
const FEISHU_WEBHOOK_ORIGIN_PATH = "/feishu/event";

const json = (body: Record<string, unknown>, status = 200): Response =>
  Response.json(body, {
    status,
    headers: { "cache-control": "no-store" },
  });

async function handleFeishuChallenge(request: Request, url: URL): Promise<Response | null> {
  if (request.method !== "POST" || url.pathname !== FEISHU_WEBHOOK_PUBLIC_PATH) {
    return null;
  }

  try {
    const body: unknown = await request.clone().json();
    if (
      typeof body === "object" &&
      body !== null &&
      "challenge" in body &&
      typeof body.challenge === "string"
    ) {
      // Feishu's developer-server URL verification must complete quickly. Handle
      // it at the edge so a sleeping or unreachable Python origin cannot cause a
      // verification timeout.
      return json({ challenge: body.challenge });
    }
  } catch {
    // Non-JSON requests continue to the normal upstream path and receive its
    // standard acknowledgement.
  }

  return null;
}

function upstreamUrl(requestUrl: URL, originValue: string): URL {
  let origin: URL;
  try {
    origin = new URL(originValue);
  } catch {
    throw new Error("PYTHON_ORIGIN must be an absolute http(s) URL");
  }

  if (origin.protocol !== "http:" && origin.protocol !== "https:") {
    throw new Error("PYTHON_ORIGIN must use http or https");
  }

  // The request path is controlled by this Worker; the client cannot change
  // the upstream host or protocol. Preserve the query for MCP/OAuth flows.
  const path =
    requestUrl.pathname === FEISHU_WEBHOOK_PUBLIC_PATH
      ? FEISHU_WEBHOOK_ORIGIN_PATH
      : requestUrl.pathname;
  const basePath = origin.pathname.replace(/\/+$/, "");
  origin.pathname = `${basePath}${path}` || "/";
  origin.search = requestUrl.search;
  return origin;
}

function forwardedRequest(request: Request, target: URL): Request {
  const headers = new Headers(request.headers);
  headers.set("x-forwarded-host", new URL(request.url).host);
  headers.set("x-forwarded-proto", "https");
  headers.delete("host");

  return new Request(target, {
    method: request.method,
    headers,
    body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    redirect: "manual",
  });
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/health" && request.method === "GET") {
      return json({ ok: true, service: "feishu2agents-worker" });
    }

    const challengeResponse = await handleFeishuChallenge(request, url);
    if (challengeResponse) {
      return challengeResponse;
    }

    if (!env.PYTHON_ORIGIN?.trim()) {
      return json(
        {
          ok: false,
          error: "PYTHON_ORIGIN is not configured",
        },
        503,
      );
    }

    let target: URL;
    try {
      target = upstreamUrl(url, env.PYTHON_ORIGIN.trim());
    } catch (error) {
      return json(
        {
          ok: false,
          error: error instanceof Error ? error.message : "Invalid PYTHON_ORIGIN",
        },
        500,
      );
    }

    // The Worker is the public edge entrypoint. Pointing PYTHON_ORIGIN back at
    // this same origin would make the Worker fetch itself recursively instead
    // of reaching the Python service. Fail early with a configuration error so
    // this cannot become an opaque 530/504 loop in production.
    if (target.origin === url.origin) {
      return json(
        {
          ok: false,
          error:
            "PYTHON_ORIGIN must be a separate Python service origin; it cannot be the Worker public origin",
        },
        500,
      );
    }

    try {
      return await fetch(forwardedRequest(request, target));
    } catch (error) {
      console.error("Python upstream request failed", error);
      return json(
        {
          ok: false,
          error: "Python upstream is unavailable",
        },
        502,
      );
    }
  },
};
