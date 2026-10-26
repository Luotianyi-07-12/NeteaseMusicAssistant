// Bind only to loopback. Authentication data should not be exposed on the LAN.
const path = require('node:path');
const fs = require('node:fs');
const state = process.env.NCM_API_TEMP_PATH || path.resolve(__dirname, '../.state/api-cache');
fs.mkdirSync(state, { recursive: true });
// The upstream module uses os.tmpdir() for anonymous_token and device IDs.
process.env.TEMP = state;
process.env.TMP = state;
process.env.TMPDIR = state;
const { serveNcmApi } = require('@neteasecloudmusicapienhanced/api');
serveNcmApi({ port: 3000, host: '127.0.0.1' }).catch(() => {
  console.error('Local API startup failed. Check Node.js version and dependencies.');
  process.exitCode = 1;
});
