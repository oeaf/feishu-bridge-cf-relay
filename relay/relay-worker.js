// Feishu bridge relay Worker (Cloudflare Worker + KV), file-based.
//
// One Worker serves many bridges; every key is namespaced by <slug>
// so bridges never collide. The Worker is a dumb passive relay: it
// stores queue files and the daemon heartbeat, nothing else. No
// Feishu credentials ever pass through it.
//
// Routes (all except /health require auth):
//   GET  /health                  -> {"ok":true,"service":"feishu-bridge-relay"}
//   PUT  /q/<slug>/<file>         -> store one queue file (body <= 400 KB)
//   GET  /q/<slug>/<file>         -> fetch one queue file (raw bytes)
//   GET  /list/<slug>             -> [{"key": "<file>", "size": <bytes>}, ...]
//   POST /del/<slug>/<file>       -> delete one queue file (the ACK step)
//   PUT  /hbfile/<slug>           -> store the daemon heartbeat JSON
//   GET  /hb/<slug>               -> fetch the daemon heartbeat JSON
//
// Auth: header  Authorization: Bearer <RELAY_TOKEN>
//   - env.RELAY_TOKEN: single token (simple deployments), or
//   - env.BRIDGE_TOKENS: JSON map {"<slug>": "<token>", ...} (multi-bridge;
//     a slug's token only ever authorizes that slug).
// KV binding name: RELAY_KV. KV keys: "q:<slug>:<file>", "hb:<slug>".
//
// Remember (guide section 5.1): a new Worker's workers.dev route is
// DISABLED by default and the API cannot enable it (error 1042 /
// 10405). Open it in the console: Workers & Pages -> this Worker ->
// Domains -> Production Worker URL -> On.

const MAX_BODY = 400 * 1024; // 400 KB
const SLUG_RE = /^[a-z0-9-]+$/;
const FILE_RE = /^[A-Za-z0-9._-]+$/;

function json(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function authorized(request, env, slug) {
  const header = request.headers.get("authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (!token) return false;
  if (env.BRIDGE_TOKENS) {
    try {
      const map = JSON.parse(env.BRIDGE_TOKENS);
      return typeof map[slug] === "string" && map[slug] === token;
    } catch {
      return false;
    }
  }
  return typeof env.RELAY_TOKEN === "string" && env.RELAY_TOKEN === token;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const parts = url.pathname.split("/").filter((p) => p.length > 0);

    if (request.method === "GET" && parts.length === 1 && parts[0] === "health") {
      return json({ ok: true, service: "feishu-bridge-relay" });
    }
    if (!env.RELAY_KV) {
      return json({ ok: false, error: "RELAY_KV binding missing" }, 500);
    }

    const [route, slug, file] = parts;
    const known = ["q", "list", "del", "hb", "hbfile"].includes(route);
    if (!known || !slug || !SLUG_RE.test(slug)) {
      return json({ ok: false, error: "bad route or slug" }, 404);
    }
    if ((route === "q" || route === "del") &&
        (!file || !FILE_RE.test(file) || file.includes(".."))) {
      return json({ ok: false, error: "bad file name" }, 400);
    }
    if (!authorized(request, env, slug)) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }

    const qkey = `q:${slug}:${file}`;

    if (route === "q" && request.method === "PUT") {
      const body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY) {
        return json({ ok: false, error: "body too large", limit: MAX_BODY }, 413);
      }
      await env.RELAY_KV.put(qkey, body, { metadata: { size: body.byteLength } });
      return json({ ok: true, key: file, size: body.byteLength });
    }
    if (route === "q" && request.method === "GET") {
      const value = await env.RELAY_KV.get(qkey, "arrayBuffer");
      if (value === null) return json({ ok: false, error: "not found" }, 404);
      return new Response(value, {
        headers: { "content-type": "application/octet-stream" },
      });
    }
    if (route === "list" && request.method === "GET") {
      const out = [];
      let cursor;
      do {
        const page = await env.RELAY_KV.list({ prefix: `q:${slug}:`, cursor });
        for (const k of page.keys) {
          out.push({
            key: k.name.slice(`q:${slug}:`.length),
            size: (k.metadata && k.metadata.size) || 0,
          });
        }
        cursor = page.list_complete ? undefined : page.cursor;
      } while (cursor);
      return json(out);
    }
    if (route === "del" && request.method === "POST") {
      await env.RELAY_KV.delete(qkey);
      return json({ ok: true, key: file });
    }
    if (route === "hbfile" && request.method === "PUT") {
      const body = await request.arrayBuffer();
      if (body.byteLength > MAX_BODY) {
        return json({ ok: false, error: "body too large", limit: MAX_BODY }, 413);
      }
      await env.RELAY_KV.put(`hb:${slug}`, body);
      return json({ ok: true });
    }
    if (route === "hb" && request.method === "GET") {
      const value = await env.RELAY_KV.get(`hb:${slug}`);
      if (value === null) return json({ ok: false, error: "no heartbeat" }, 404);
      return new Response(value, {
        headers: { "content-type": "application/json" },
      });
    }
    return json({ ok: false, error: "method not allowed" }, 405);
  },
};
