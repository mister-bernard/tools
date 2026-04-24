import fs from 'node:fs';
import path from 'node:path';
import { log } from './log.js';

// Structured origin metadata for Telegram outbound messages.
//
// Flow: agent sends TG msg via tg-send-logged.sh → script POSTs
// {chat_id, msg_id, origin} to Telegraph's /origin endpoint → record lands here.
//
// On inbound reply, handleMessage reads replyContext.messageId and
// calls lookup(chat_id, reply_to_msg_id). If hit, we dispatch to the
// actual originator instead of the static binding table.

export class OriginStore {
  constructor({ file, maxRecent = 5000 }) {
    this._file = file;
    this._max = maxRecent;
    this._map = new Map();
    this._loaded = false;
  }

  load() {
    if (this._loaded) return;
    this._loaded = true;

    if (!fs.existsSync(this._file)) {
      fs.mkdirSync(path.dirname(this._file), { recursive: true });
      return;
    }

    const raw = fs.readFileSync(this._file, 'utf8');
    const lines = raw.split('\n').filter(l => l.trim());
    const tail = lines.slice(-this._max);

    for (const line of tail) {
      try {
        const rec = JSON.parse(line);
        if (rec.chat_id != null && rec.msg_id != null && rec.origin) {
          this._map.set(this._key(rec.chat_id, rec.msg_id), rec.origin);
        }
      } catch {
        // skip malformed
      }
    }

    log.info('origin-store loaded', { records: this._map.size, file: this._file });
  }

  _key(chatId, msgId) {
    return `${chatId}:${msgId}`;
  }

  // Register a new outbound message → origin mapping.
  // Appends to jsonl, adds to map, evicts oldest if over capacity.
  register({ chatId, msgId, origin }) {
    if (!origin || typeof origin !== 'object' || !origin.type) {
      throw new Error('origin must be object with .type');
    }

    const rec = {
      ts: new Date().toISOString(),
      chat_id: chatId,
      msg_id: msgId,
      origin,
    };

    fs.appendFileSync(this._file, JSON.stringify(rec) + '\n');

    this._map.set(this._key(chatId, msgId), origin);

    if (this._map.size > this._max) {
      const firstKey = this._map.keys().next().value;
      this._map.delete(firstKey);
    }

    return rec;
  }

  // Returns origin object or null.
  lookup(chatId, msgId) {
    if (chatId == null || msgId == null) return null;
    return this._map.get(this._key(chatId, msgId)) || null;
  }

  size() {
    return this._map.size;
  }
}
