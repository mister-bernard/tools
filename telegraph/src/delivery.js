import fs from 'node:fs';
import path from 'node:path';
import * as tg from './tg-api.js';
import { log } from './log.js';

const CHUNK_SIZE = 4000;
const CAPTION_LIMIT = 1024;
const NO_REPLY_PATTERNS = ['NO_REPLY', 'HEARTBEAT_OK'];
const PHOTO_EXTS = new Set(['.jpg', '.jpeg', '.png', '.gif', '.webp']);

export class Delivery {
  constructor({ parseMode = 'HTML' } = {}) {
    this._parseMode = parseMode;
  }

  shouldSkip(text) {
    if (!text || !text.trim()) return true;
    return NO_REPLY_PATTERNS.some(p => text.trim().startsWith(p));
  }

  escapeHtml(text) {
    return text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  mdToHtml(text) {
    const codeBlocks = [];
    let escaped = text.replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
      const idx = codeBlocks.length;
      codeBlocks.push(`<pre><code>${this.escapeHtml(code)}</code></pre>`);
      return `\x00CODEBLOCK${idx}\x00`;
    });

    const inlineCodes = [];
    escaped = escaped.replace(/`([^`]+)`/g, (_, code) => {
      const idx = inlineCodes.length;
      inlineCodes.push(`<code>${this.escapeHtml(code)}</code>`);
      return `\x00INLINE${idx}\x00`;
    });

    escaped = this.escapeHtml(escaped);

    escaped = escaped.replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
    escaped = escaped.replace(/(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)/g, '<i>$1</i>');
    escaped = escaped.replace(/~~(.+?)~~/g, '<s>$1</s>');
    escaped = escaped.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');

    escaped = escaped.replace(/\x00CODEBLOCK(\d+)\x00/g, (_, i) => codeBlocks[i]);
    escaped = escaped.replace(/\x00INLINE(\d+)\x00/g, (_, i) => inlineCodes[i]);

    return escaped;
  }

  chunk(text) {
    if (text.length <= CHUNK_SIZE) return [text];
    const chunks = [];
    let remaining = text;
    while (remaining.length > 0) {
      if (remaining.length <= CHUNK_SIZE) {
        chunks.push(remaining);
        break;
      }
      let splitAt = this._findSafeSplit(remaining, CHUNK_SIZE);
      chunks.push(remaining.slice(0, splitAt));
      remaining = remaining.slice(splitAt).replace(/^\n+/, '');
    }
    return chunks;
  }

  _findSafeSplit(text, maxLen) {
    let pos = text.lastIndexOf('\n\n', maxLen);
    if (pos < maxLen * 0.3) pos = text.lastIndexOf('\n', maxLen);
    if (pos < maxLen * 0.3) pos = maxLen;

    // don't split inside an HTML entity (&amp; &#123; etc.)
    const tail = text.slice(Math.max(0, pos - 10), pos);
    const ampIdx = tail.lastIndexOf('&');
    if (ampIdx >= 0 && !tail.slice(ampIdx).includes(';')) {
      pos = Math.max(0, pos - 10) + ampIdx;
    }

    // don't split inside an HTML tag
    const tagTail = text.slice(Math.max(0, pos - 50), pos);
    const openTag = tagTail.lastIndexOf('<');
    const closeTag = tagTail.lastIndexOf('>');
    if (openTag > closeTag) {
      pos = Math.max(0, pos - 50) + openTag;
    }

    return pos;
  }

  extractFilePaths(text) {
    const root = (process.env.TELEGRAPH_ATTACHMENT_ROOT || '/').replace(/\/+$/, '');
    const anchor = root === '' ? '\\/' : root.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\/';
    const re = new RegExp(`(?:^|\\s)(${anchor}[^\\s]+\\.(?:pdf|png|jpg|jpeg|csv|xlsx|zip|tar\\.gz|mp3|mp4|json|txt))`, 'gm');
    const paths = [];
    let m;
    while ((m = re.exec(text)) !== null) {
      const p = m[1];
      if (fs.existsSync(p)) paths.push(p);
    }
    return paths;
  }

  async send(token, chatId, text, { replyTo, threadId } = {}) {
    if (this.shouldSkip(text)) {
      log.debug('skipping delivery (no-reply)', { chatId });
      return [];
    }

    const html = this.mdToHtml(text);
    const chunks = this.chunk(html);
    const sentIds = [];

    for (let i = 0; i < chunks.length; i++) {
      try {
        const msg = await tg.sendMessage(token, chatId, chunks[i], {
          parseMode: this._parseMode,
          replyTo: i === 0 ? replyTo : undefined,
          disableLinkPreview: true,
          threadId,
        });
        sentIds.push(msg.message_id);
      } catch (e) {
        if (e.message?.includes('parse') || e.message?.includes("can't parse")) {
          log.warn('HTML parse failed, retrying plain text', { chatId });
          const plainChunks = this._plainChunk(text);
          for (const pc of plainChunks) {
            const msg = await tg.sendMessage(token, chatId, pc, {
              replyTo: sentIds.length === 0 ? replyTo : undefined,
              threadId,
            });
            sentIds.push(msg.message_id);
          }
          return sentIds;
        }
        throw e;
      }
    }

    const filePaths = this.extractFilePaths(text);
    for (const fp of filePaths) {
      try {
        const ext = path.extname(fp).toLowerCase();
        if (PHOTO_EXTS.has(ext)) {
          await tg.sendPhoto(token, chatId, fp, { threadId });
        } else {
          await tg.sendDocument(token, chatId, fp, { threadId });
        }
        log.info('sent file', { chatId, file: fp });
      } catch (e) {
        log.error('file send failed', { chatId, file: fp, error: e.message });
      }
    }

    return sentIds;
  }

  _plainChunk(text) {
    const chunks = [];
    for (let i = 0; i < text.length; i += CHUNK_SIZE) {
      chunks.push(text.slice(i, i + CHUNK_SIZE));
    }
    return chunks;
  }
}
