import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { relative, resolve, sep } from "node:path";

const configuredDirectory = process.env.MAAIS_DASHBOARD_OUT_DIR || "dist";
if (!new Set(["dist", "dist-sourcemaps"]).has(configuredDirectory)) {
  throw new Error("MAAIS_DASHBOARD_OUT_DIR must be dist or dist-sourcemaps");
}
const directory = resolve(process.cwd(), configuredDirectory);
const releaseValue = (
  process.env.VITE_SENTRY_RELEASE
  || process.env.SENTRY_RELEASE
  || process.env.RAILWAY_GIT_COMMIT_SHA
  || ""
).trim();
if (releaseValue && !/^[0-9a-f]{40}$/.test(releaseValue)) {
  throw new Error("dashboard asset release must be one lowercase 40-character Git SHA");
}

const manifestPath = resolve(directory, "asset-manifest.json");
const paths = await walk(directory);
const assets = [];
for (const path of paths) {
  if (path === manifestPath) continue;
  const assetPath = relative(directory, path).split(sep).join("/");
  if (assetPath.endsWith(".map")) {
    throw new Error(`deployable dashboard contains source map: ${assetPath}`);
  }
  const content = await readFile(path);
  if (/\.(?:css|js)$/.test(assetPath) && content.includes("sourceMappingURL=")) {
    throw new Error(`deployable dashboard references a source map: ${assetPath}`);
  }
  if (content.includes("SENTRY_AUTH_TOKEN")) {
    throw new Error(`deployable dashboard contains a forbidden build secret marker: ${assetPath}`);
  }
  assets.push({
    path: assetPath,
    sha256: createHash("sha256").update(content).digest("hex"),
    size: content.length,
  });
}
assets.sort((left, right) => left.path.localeCompare(right.path));
if (assets.length === 0) throw new Error("dashboard asset inventory cannot be empty");

const payload = {
  schema_version: 1,
  release: releaseValue || null,
  assets,
};
const manifest = {
  ...payload,
  manifest_hash: createHash("sha256").update(JSON.stringify(payload)).digest("hex"),
};
await writeFile(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`, {
  encoding: "utf8",
  flag: "w",
  mode: 0o644,
});

async function walk(root) {
  const entries = await readdir(root, { withFileTypes: true });
  const results = [];
  for (const entry of entries) {
    const path = resolve(root, entry.name);
    if (entry.isSymbolicLink()) throw new Error(`dashboard assets forbid symlinks: ${path}`);
    if (entry.isDirectory()) results.push(...await walk(path));
    else if (entry.isFile()) {
      const metadata = await stat(path);
      if (!metadata.isFile()) throw new Error(`dashboard asset is not a regular file: ${path}`);
      results.push(path);
    }
  }
  return results;
}
