// PM2 Ecosystem Configuration for Honcho MCP Server
// Cloudflare Worker (wrangler dev) that bridges Claude Desktop / MCP clients to
// the self-hosted Honcho API on http://127.0.0.1:8500 (HONCHO_API_URL is read
// from mcp/.dev.vars). Run: pm2 start ecosystem.config.cjs && pm2 save
module.exports = {
  apps: [
    {
      name: 'honcho-mcp',
      // Run wrangler DIRECTLY under node (not via `bun run wrangler`). bun
      // spawns wrangler as a grandchild and detaches, so PM2 would track the
      // wrong process — the real long-lived server (node→wrangler→workerd)
      // orphans, keeps holding port 8501, and every PM2 restart then fails with
      // "Address already in use". Pointing PM2 at the node entry directly means
      // PM2 controls the actual process and tears down workerd on stop/restart.
      script: './node_modules/wrangler/bin/wrangler.js',
      args: 'dev --port 8501 --ip 127.0.0.1',
      cwd: '/Users/robsherman/Servers/honcho/mcp',
      interpreter: '/opt/homebrew/bin/node',
      instances: 1,
      exec_mode: 'fork',
      watch: false,
      env: {
        WRANGLER_SEND_METRICS: 'false',
        CI: '1',
      },

      // Logging
      log_file: './logs/honcho-mcp.log',
      error_file: './logs/honcho-mcp-error.log',
      out_file: './logs/honcho-mcp-out.log',
      log_date_format: 'YYYY-MM-DD HH:mm:ss Z',
      merge_logs: true,

      // Auto-restart
      autorestart: true,
      restart_delay: 4000,
      max_restarts: 10,
      min_uptime: '10s',

      // Memory limit
      max_memory_restart: '300M',

      // Process management
      kill_timeout: 5000,
      listen_timeout: 8000,
      exp_backoff_restart_delay: 100,
    },
  ],
};
