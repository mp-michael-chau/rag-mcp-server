import type { NextConfig } from "next";

// Rewrites are baked into the routes manifest at build time, so API_ORIGIN is a
// build-time setting (docker-compose passes it as a build arg; default = local dev).
const apiOrigin = process.env.API_ORIGIN ?? "http://127.0.0.1:8100";

const nextConfig: NextConfig = {
  output: "standalone",
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${apiOrigin}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
