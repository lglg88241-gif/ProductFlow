import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "");
  const devPort = Number(env.WEB_PORT || 29283);
  const previewPort = Number(env.WEB_PORT || 29281);
  const devProxyTarget = env.VITE_DEV_PROXY_TARGET || "http://127.0.0.1:29282";
  const allowedHosts = env.WEB_ALLOWED_HOSTS
    ? env.WEB_ALLOWED_HOSTS.split(",").map((host) => host.trim()).filter(Boolean)
    : ["draw.devbin.de"];

  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: devPort,
      strictPort: true,
      host: "0.0.0.0",
      allowedHosts,
      proxy: {
        "/api": {
          target: devProxyTarget,
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: previewPort,
      strictPort: true,
      host: "0.0.0.0",
      allowedHosts,
    },
    build: {
      rollupOptions: {
        output: {
          // 按生态拆分第三方依赖，稳定缓存：react 全家桶一组、@tanstack 一组、lucide 图标一组
          manualChunks(id: string) {
            if (id.indexOf("node_modules") === -1) return undefined;
            if (id.indexOf("@tanstack") !== -1) return "tanstack";
            if (id.indexOf("lucide-react") !== -1) return "lucide";
            if (/[\\/]node_modules[\\/](react|react-dom|react-router|react-router-dom|scheduler)[\\/]/.test(id)) {
              return "react";
            }
            return undefined;
          },
        },
      },
    },
  };
});
