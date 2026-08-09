import react from "@vitejs/plugin-react";
import { sentryVitePlugin } from "@sentry/vite-plugin";
import { defineConfig, loadEnv } from "vite";

const RELEASE_PATTERN = /^[0-9a-f]{40}$/;
const SOURCE_MAP_PROJECT = "maais-mission-control";

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, ".", "");
  const sourceMapUpload = environment.MAAIS_SOURCE_MAP_UPLOAD === "true";
  const outDir = environment.MAAIS_DASHBOARD_OUT_DIR || "dist";
  if (!new Set(["dist", "dist-sourcemaps"]).has(outDir)) {
    throw new Error("MAAIS_DASHBOARD_OUT_DIR must be dist or dist-sourcemaps");
  }
  const plugins = [react()];
  if (sourceMapUpload) {
    const release = environment.SENTRY_RELEASE || "";
    const authToken = environment.SENTRY_AUTH_TOKEN || "";
    const org = environment.SENTRY_ORG || "";
    if (!RELEASE_PATTERN.test(release) || !authToken || !org || outDir !== "dist-sourcemaps") {
      throw new Error(
        "source-map upload requires exact release, auth token, organization, and dist-sourcemaps",
      );
    }
    plugins.push(sentryVitePlugin({
      authToken,
      org,
      project: SOURCE_MAP_PROJECT,
      telemetry: false,
      sourcemaps: {
        assets: `./${outDir}/**`,
        filesToDeleteAfterUpload: `./${outDir}/**/*.map`,
      },
      release: {
        name: release,
        create: true,
        finalize: true,
        inject: true,
        setCommits: false,
        deploy: false,
      },
      bundleSizeOptimizations: {
        excludeDebugStatements: true,
        excludeReplayIframe: true,
        excludeReplayShadowDom: true,
        excludeReplayWorker: true,
      },
    }));
  }

  return {
    plugins,
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8000",
          ws: true,
        },
      },
    },
    build: {
      outDir,
      emptyOutDir: true,
      sourcemap: sourceMapUpload ? "hidden" : false,
    },
  };
});
