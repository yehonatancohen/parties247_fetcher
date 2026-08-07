const ALLOWED_PREFIX = "https://www.go-out.co/endOne/";

export default {
  async fetch(request, env) {
    if (request.headers.get("x-relay-secret") !== env.RELAY_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const url = new URL(request.url);
    const target = url.searchParams.get("target");
    if (!target || !target.startsWith(ALLOWED_PREFIX)) {
      return new Response("bad target", { status: 400 });
    }

    const cookie = request.headers.get("x-relay-cookie") || "";
    const auth = request.headers.get("x-relay-auth") || "";
    const bodyText = request.method === "POST" ? await request.text() : undefined;

    try {
      const upstream = await fetch(target, {
        method: "POST",
        headers: {
          "User-Agent": "Mozilla/5.0",
          "content-type": "application/json",
          "Origin": "https://www.go-out.co",
          "Referer": "https://www.go-out.co/businesspage",
          ...(cookie ? { cookie } : {}),
          ...(auth ? { authorization: auth } : {}),
        },
        body: bodyText && bodyText.length ? bodyText : "{}",
      });
      const body = await upstream.text();
      return new Response(body, {
        status: upstream.status,
        headers: { "content-type": upstream.headers.get("content-type") || "application/json" },
      });
    } catch (e) {
      return new Response(JSON.stringify({ ok: false, error: String(e) }), {
        status: 502,
        headers: { "content-type": "application/json" },
      });
    }
  },
};
