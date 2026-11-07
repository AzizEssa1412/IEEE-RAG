/** @type {import('next').NextConfig} */
const BACKEND = process.env.UNIGUIDE_BACKEND_URL || "http://127.0.0.1:8000";

const nextConfig = {
  // Same-origin /api/* calls are proxied to the FastAPI backend (no CORS needed).
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
  experimental: { proxyTimeout: 240000 },
};

export default nextConfig;
