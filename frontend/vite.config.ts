import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { execSync } from "node:child_process";
import { cpSync, createReadStream, existsSync, statSync } from "node:fs";
import { resolve, join, normalize } from "node:path";

// The ONE place the GitHub account is written. Everything else builds links from this.
const REPO_URL = process.env.VITE_REPO_URL ?? "https://github.com/ethaneatab/leasejobs";
function gitSha(): string {
  if (process.env.VITE_GIT_SHA) return process.env.VITE_GIT_SHA;
  try { return execSync("git rev-parse HEAD", { cwd: __dirname }).toString().trim(); } catch { return "HEAD"; }
}
const VIDEOS = resolve(__dirname, "../docs/videos");

// Serve docs/videos at /videos in dev; copy it into dist/videos on build.
function videos(): Plugin {
  return {
    name: "leasejobs-videos",
    configureServer(server) {
      server.middlewares.use("/videos", (req, res, next) => {
        const p = normalize(join(VIDEOS, decodeURIComponent((req.url ?? "/").split("?")[0])));
        if (!p.startsWith(VIDEOS) || !existsSync(p) || !statSync(p).isFile()) { res.statusCode = 404; res.end(); return; }
        res.setHeader("content-type", p.endsWith(".mp4") ? "video/mp4" : "application/octet-stream");
        res.setHeader("content-length", String(statSync(p).size));
        if (req.method === "HEAD") { res.end(); return; }
        createReadStream(p).pipe(res);
      });
    },
    closeBundle() {
      if (existsSync(VIDEOS)) cpSync(VIDEOS, resolve(__dirname, "dist/videos"), { recursive: true });
    },
  };
}

export default defineConfig({
  plugins: [react(), videos()],
  define: {
    "import.meta.env.VITE_REPO_URL": JSON.stringify(REPO_URL),
    "import.meta.env.VITE_GIT_SHA": JSON.stringify(gitSha()),
  },
  server: {
    host: "127.0.0.1",
    port: 5191,
    strictPort: true,
    fs: { allow: [resolve(__dirname, "..")] }, // docs/*.md are imported with ?raw
    proxy: { "/api": { target: "http://127.0.0.1:8910", rewrite: (p) => p.replace(/^\/api/, "") } },
  },
  preview: { host: "127.0.0.1", port: 5191, proxy: { "/api": { target: "http://127.0.0.1:8910", rewrite: (p) => p.replace(/^\/api/, "") } } },
});
