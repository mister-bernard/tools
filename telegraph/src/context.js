import fs from 'node:fs';
import crypto from 'node:crypto';
import { log } from './log.js';

export class ContextTracker {
  constructor(filePath) {
    this._filePath = filePath;
    this._hashes = {};
    this._content = '';
    this._contentHash = '';
    this._watcher = null;
    this._reload();
  }

  start() {
    if (!this._filePath) return;
    try {
      this._watcher = fs.watch(this._filePath, () => {
        this._reload();
      });
      log.info('watching shared-context', { file: this._filePath });
    } catch (e) {
      log.warn('cannot watch shared-context', { error: e.message });
    }
  }

  stop() {
    this._watcher?.close();
  }

  _reload() {
    try {
      this._content = fs.readFileSync(this._filePath, 'utf8');
      this._contentHash = crypto.createHash('sha256').update(this._content).digest('hex').slice(0, 16);
    } catch {
      this._content = '';
      this._contentHash = '';
    }
  }

  getDelta(sessionId) {
    if (!this._content) return null;
    const lastHash = this._hashes[sessionId];
    if (lastHash === this._contentHash) return null;
    return this._content;
  }

  markSeen(sessionId) {
    this._hashes[sessionId] = this._contentHash;
  }
}
