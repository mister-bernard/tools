import http from 'node:http';
import path from 'node:path';
import fs from 'node:fs';
import { log } from './log.js';

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

function rpc(url, method, params = {}) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const body = JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method, params });
    const opts = {
      hostname: u.hostname,
      port: parseInt(u.port, 10),
      path: u.pathname,
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    };
    const req = http.request(opts, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => {
        try {
          const json = JSON.parse(buf);
          if (json.error) reject(new Error(`signal-cli RPC ${method}: ${json.error.message || JSON.stringify(json.error)}`));
          else resolve(json.result);
        } catch (e) { reject(new Error(`signal-cli RPC bad response: ${buf.slice(0, 200)}`)); }
      });
    });
    req.on('error', reject);
    req.setTimeout(30_000, () => { req.destroy(new Error('timeout')); });
    req.write(body);
    req.end();
  });
}

export class SignalPoller {
  constructor({ account, rpcUrl, onMessage, pollIntervalMs = 2000, attachmentDir }) {
    this._account = account;
    this._rpcUrl = rpcUrl;
    this._onMessage = onMessage;
    this._pollIntervalMs = pollIntervalMs;
    this._attachmentDir = attachmentDir || '${HOME}/.local/share/signal-cli/attachments';
    this._running = false;
  }

  async start() {
    this._running = true;
    log.info('signal poller started', { account: this._account, rpc: this._rpcUrl });
    while (this._running) {
      try {
        await this._poll();
      } catch (e) {
        log.error('signal poll error', { error: e.message });
      }
      await sleep(this._pollIntervalMs);
    }
  }

  stop() { this._running = false; }

  async _poll() {
    const envelopes = await rpc(this._rpcUrl, 'receive', { timeout: 1 });
    if (!Array.isArray(envelopes)) return;

    for (const env of envelopes) {
      const parsed = this._parseEnvelope(env);
      if (!parsed) continue;

      log.info('signal message received', {
        from: parsed.userId,
        len: (parsed.text || '').length,
        group: parsed.chatType === 'group' ? parsed.chatId : undefined,
      });

      try {
        await this._onMessage(parsed);
      } catch (e) {
        log.error('signal message handler error', { error: e.message });
      }
    }
  }

  _parseEnvelope(env) {
    const envelope = env.envelope || env;
    if (!envelope) return null;

    const source = envelope.sourceNumber || envelope.source;
    if (!source) return null;

    const dataMsg = envelope.dataMessage;
    if (!dataMsg) return null;

    const groupInfo = dataMsg.groupInfo;
    const chatType = groupInfo ? 'group' : 'dm';
    const chatId = groupInfo?.groupId || source;
    const text = dataMsg.message || '';

    const mediaPaths = [];
    const mediaTypes = [];
    if (dataMsg.attachments?.length) {
      for (const att of dataMsg.attachments) {
        const attPath = att.id ? path.join(this._attachmentDir, att.id) : null;
        if (attPath && fs.existsSync(attPath)) {
          mediaPaths.push(attPath);
          const type = (att.contentType || '').startsWith('image/') ? 'photo'
            : (att.contentType || '').startsWith('audio/') ? 'voice'
            : (att.contentType || '').startsWith('video/') ? 'video'
            : 'document';
          mediaTypes.push(type);
        }
      }
    }

    if (!text && mediaPaths.length === 0) return null;

    const username = envelope.sourceName || source;

    return {
      accountId: 'signal',
      chatType,
      chatId,
      userId: source,
      messageId: envelope.timestamp || Date.now(),
      username,
      text,
      mediaPaths,
      mediaTypes,
      forward: null,
      replyContext: dataMsg.quote ? {
        kind: 'quote',
        sender: dataMsg.quote.authorNumber || 'unknown',
        quoteText: dataMsg.quote.text || '',
      } : null,
      location: null,
      threadId: null,
      isForum: false,
      edited: false,
      ts: new Date(envelope.timestamp || Date.now()).toISOString(),
    };
  }

  async send(recipient, text) {
    return rpc(this._rpcUrl, 'send', {
      recipient: [recipient],
      message: text,
      account: this._account,
    });
  }

  async sendGroupMessage(groupId, text) {
    return rpc(this._rpcUrl, 'send', {
      groupId,
      message: text,
      account: this._account,
    });
  }

  async sendAttachment(recipient, filePath, message = '') {
    return rpc(this._rpcUrl, 'send', {
      recipient: [recipient],
      message,
      attachments: [filePath],
      account: this._account,
    });
  }
}
