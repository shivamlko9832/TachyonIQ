/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  webpack: (config) => {
    // vega (via vega-embed -> ChartView) optionally requires the native
    // "canvas" package for server-side/Node rendering; ChartView only ever
    // runs vega-embed client-side with the svg renderer, so this is safe to
    // stub out and keeps the dev/build logs free of a "module not found"
    // warning that has no effect on behaviour.
    config.resolve.fallback = { ...config.resolve.fallback, canvas: false };
    return config;
  },
};

export default nextConfig;
