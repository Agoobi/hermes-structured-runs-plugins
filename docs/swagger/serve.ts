#!/usr/bin/env bun
/**
 * Validate docs/openapi.json, then serve Swagger UI for it.
 *
 *   bun run swagger                -> validate + serve on :8677
 *   bun run swagger -- 9000        -> serve on :9000
 *   bun run swagger -- --lint      -> validate only, exit (CI-friendly)
 *
 * Swagger UI assets come from the `swagger-ui-dist` package (installed by the
 * `preswagger` step), so this works offline.
 */
import { absolutePath } from "swagger-ui-dist";
import { join } from "node:path";

const SPEC_PATH = join(import.meta.dir, "..", "openapi.json");
const INDEX_PATH = join(import.meta.dir, "index.html");
const ASSET_DIR = absolutePath();

const args = process.argv.slice(2);
const lintOnly = args.includes("--lint");
const portArg = args.find((a) => /^\d+$/.test(a));
const PORT = Number(portArg ?? Bun.env.SWAGGER_PORT ?? 8677);

// --- validate --------------------------------------------------------------
let spec: any;
try {
  spec = await Bun.file(SPEC_PATH).json();
} catch (err) {
  console.error(`✖ docs/openapi.json is not valid JSON: ${(err as Error).message}`);
  process.exit(1);
}

const problems: string[] = [];
if (typeof spec.openapi !== "string") problems.push('missing top-level "openapi" version string');
if (!spec.info?.title) problems.push("missing info.title");
if (!spec.paths || typeof spec.paths !== "object") problems.push('missing "paths" object');

const METHODS = ["get", "post", "put", "patch", "delete"];
const ops: string[] = [];
const seenIds = new Set<string>();
for (const [p, item] of Object.entries<any>(spec.paths ?? {})) {
  for (const m of Object.keys(item)) {
    if (!METHODS.includes(m)) continue;
    const op = item[m];
    ops.push(`${m.toUpperCase().padEnd(6)} ${p}`);
    const id = op.operationId;
    if (id) {
      if (seenIds.has(id)) problems.push(`duplicate operationId "${id}"`);
      seenIds.add(id);
    }
    for (const ref of JSON.stringify(op).matchAll(/"\$ref":\s*"(#[^"]+)"/g)) {
      const path = ref[1].slice(2).split("/");
      let node: any = spec;
      for (const seg of path) node = node?.[seg.replace(/~1/g, "/").replace(/~0/g, "~")];
      if (node === undefined) problems.push(`${m.toUpperCase()} ${p}: dangling $ref ${ref[1]}`);
    }
  }
}

if (problems.length) {
  console.error("✖ openapi.json:\n  - " + problems.join("\n  - "));
  process.exit(1);
}

console.log(`✔ openapi ${spec.openapi} — "${spec.info.title}" v${spec.info.version ?? "?"}`);
console.log(`  ${ops.length} operations:`);
for (const o of ops.sort()) console.log(`    ${o}`);

if (lintOnly) process.exit(0);

// --- serve ----------------------------------------------------------------
const server = Bun.serve({
  port: PORT,
  async fetch(req) {
    const { pathname } = new URL(req.url);
    if (pathname === "/" || pathname === "/index.html") {
      return new Response(Bun.file(INDEX_PATH), {
        headers: { "content-type": "text/html; charset=utf-8" },
      });
    }
    if (pathname === "/openapi.json") {
      return new Response(JSON.stringify(spec), {
        headers: { "content-type": "application/json" },
      });
    }
    const asset = Bun.file(join(ASSET_DIR, pathname.replace(/^\/+/, "").replace(/\.\.+/g, "")));
    if (await asset.exists()) return new Response(asset);
    return new Response("not found", { status: 404 });
  },
});

console.log(`\n➜  Swagger UI:  http://localhost:${server.port}`);
console.log(`➜  Spec:        http://localhost:${server.port}/openapi.json`);
console.log("   (Ctrl+C to stop)");
