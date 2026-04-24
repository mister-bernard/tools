import http from 'node:http';
import { log } from './log.js';

export class BridgeClient {
  constructor({ baseUrl, bearer }) {
    const u = new URL(baseUrl);
    this._host = u.hostname;
    this._port = parseInt(u.port, 10) || 80;
    this._basePath = u.pathname.replace(/\/$/, '');
    this._bearer = bearer;
  }

  async complete(sessionId, prompt, { user } = {}) {
    const body = JSON.stringify({
      model: sessionId,
      messages: [{ role: 'user', content: prompt }],
      stream: false,
      ...(user ? { user } : {}),
    });

    return new Promise((resolve, reject) => {
      const opts = {
        hostname: this._host,
        port: this._port,
        path: `${this._basePath}/chat/completions`,
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          ...(this._bearer ? { 'Authorization': `Bearer ${this._bearer}` } : {}),
        },
      };

      const req = http.request(opts, (res) => {
        let buf = '';
        res.on('data', (c) => { buf += c; });
        res.on('end', () => {
          if (res.statusCode !== 200) {
            log.error('bridge error', { status: res.statusCode, body: buf.slice(0, 500) });
            reject(new Error(`bridge HTTP ${res.statusCode}: ${buf.slice(0, 200)}`));
            return;
          }
          try {
            const json = JSON.parse(buf);
            const text = json.choices?.[0]?.message?.content || '';
            resolve({ text, raw: json });
          } catch (e) {
            reject(new Error(`bridge bad JSON: ${buf.slice(0, 200)}`));
          }
        });
      });
      req.on('error', reject);
      req.setTimeout(600_000, () => { req.destroy(new Error('bridge timeout (600s)')); });
      req.write(body);
      req.end();
    });
  }

  async health() {
    return new Promise((resolve, reject) => {
      const req = http.get({
        hostname: this._host,
        port: this._port,
        path: `${this._basePath.replace('/v1', '')}/healthz`,
      }, (res) => {
        let buf = '';
        res.on('data', (c) => { buf += c; });
        res.on('end', () => {
          try { resolve(JSON.parse(buf)); } catch { resolve({ raw: buf }); }
        });
      });
      req.on('error', reject);
      req.setTimeout(5000, () => { req.destroy(new Error('timeout')); });
    });
  }
}
