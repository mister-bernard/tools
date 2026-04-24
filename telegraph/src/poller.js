import fs from 'node:fs';
import path from 'node:path';
import * as tg from './tg-api.js';
import { downloadPhoto, downloadMedia } from './media.js';
import { log } from './log.js';

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

export class Poller {
  constructor({ accountId, token, botUsername, stateDir, onMessage, pollTimeout = 30, ackReaction, requireMention = false }) {
    this._accountId = accountId;
    this._token = token;
    this._botUsername = botUsername?.toLowerCase() || '';
    this._stateDir = stateDir;
    this._onMessage = onMessage;
    this._pollTimeout = pollTimeout;
    this._ackReaction = ackReaction || null;
    this._requireMention = requireMention;
    this._running = false;
    this._offset = this._loadOffset();
  }

  _offsetFile() {
    return path.join(this._stateDir, `offset-${this._accountId}.json`);
  }

  _loadOffset() {
    try {
      return JSON.parse(fs.readFileSync(this._offsetFile(), 'utf8')).offset || 0;
    } catch { return 0; }
  }

  _saveOffset() {
    fs.writeFileSync(this._offsetFile(), JSON.stringify({ offset: this._offset }));
  }

  async start() {
    this._running = true;
    log.info('poller started', { account: this._accountId });
    while (this._running) {
      try {
        await this._poll();
      } catch (e) {
        if (e.errorCode === 409) {
          log.warn('409 conflict — another poller active, backing off', { account: this._accountId });
          await sleep(10_000);
        } else {
          log.error('poll error', { account: this._accountId, error: e.message });
          await sleep(5000);
        }
      }
    }
  }

  stop() { this._running = false; }

  async _poll() {
    const updates = await tg.getUpdates(this._token, this._offset, this._pollTimeout);

    for (const update of updates) {
      this._offset = update.update_id + 1;
      this._saveOffset();

      const msg = update.message || update.edited_message;
      if (!msg) continue;

      const parsed = await this._parseMessage(msg, !!update.edited_message);
      if (!parsed) continue;

      if (parsed.chatType === 'group' && this._requireMention && !this._isMentioned(msg)) {
        continue;
      }

      if (this._ackReaction) {
        tg.setMessageReaction(this._token, msg.chat.id, msg.message_id, this._ackReaction).catch(() => {});
      }

      log.info('message received', {
        account: this._accountId,
        chat: parsed.chatId,
        user: parsed.username,
        len: (parsed.text || '').length,
        media: parsed.mediaTypes.length ? parsed.mediaTypes : undefined,
        edited: parsed.edited || undefined,
      });

      try {
        await this._onMessage(parsed);
      } catch (e) {
        log.error('message handler error', { chatId: parsed.chatId, error: e.message });
      }
    }
  }

  async _parseMessage(msg, edited) {
    const chatType = (msg.chat.type === 'private') ? 'dm' : 'group';
    const chatId = msg.chat.id;
    const userId = msg.from?.id;
    const messageId = msg.message_id;
    const username = msg.from?.username || msg.from?.first_name || String(userId);
    const threadId = msg.message_thread_id || null;
    const isForum = !!msg.chat.is_forum;

    let text = msg.text || msg.caption || '';
    const entities = msg.entities || msg.caption_entities || [];

    text = this._expandTextLinks(text, entities);

    const mediaPaths = [];
    const mediaTypes = [];

    if (msg.photo?.length) {
      const p = await downloadPhoto(this._token, msg.photo);
      if (p) { mediaPaths.push(p); mediaTypes.push('photo'); }
    }
    if (msg.document) {
      const p = await downloadMedia(this._token, msg.document, 'document');
      if (p) { mediaPaths.push(p); mediaTypes.push('document'); }
    }
    if (msg.voice) {
      const p = await downloadMedia(this._token, msg.voice, 'voice');
      if (p) { mediaPaths.push(p); mediaTypes.push('voice'); }
    }
    if (msg.audio) {
      const p = await downloadMedia(this._token, msg.audio, 'audio');
      if (p) { mediaPaths.push(p); mediaTypes.push('audio'); }
    }
    if (msg.video) {
      const p = await downloadMedia(this._token, msg.video, 'video');
      if (p) { mediaPaths.push(p); mediaTypes.push('video'); }
    }
    if (msg.video_note) {
      const p = await downloadMedia(this._token, msg.video_note, 'video_note');
      if (p) { mediaPaths.push(p); mediaTypes.push('video_note'); }
    }
    if (msg.sticker) {
      mediaTypes.push('sticker');
    }

    if (!text && mediaTypes.length === 0 && !msg.location && !msg.venue) return null;

    const forward = this._parseForward(msg);
    const replyContext = this._parseReply(msg);
    const location = this._parseLocation(msg);

    return {
      accountId: this._accountId,
      chatType, chatId, userId, messageId, username,
      text, mediaPaths, mediaTypes,
      forward, replyContext, location,
      threadId, isForum, edited,
      ts: new Date(msg.date * 1000).toISOString(),
    };
  }

  _expandTextLinks(text, entities) {
    if (!entities?.length) return text;
    const textLinks = entities
      .filter(e => e.type === 'text_link')
      .sort((a, b) => b.offset - a.offset);
    let expanded = text;
    for (const e of textLinks) {
      const display = expanded.slice(e.offset, e.offset + e.length);
      expanded = expanded.slice(0, e.offset) + `[${display}](${e.url})` + expanded.slice(e.offset + e.length);
    }
    return expanded;
  }

  _isMentioned(msg) {
    if (!this._botUsername) return false;
    const text = (msg.text || msg.caption || '').toLowerCase();
    if (text.includes(`@${this._botUsername}`)) return true;
    const entities = msg.entities || msg.caption_entities || [];
    for (const e of entities) {
      if (e.type === 'mention') {
        const mention = (msg.text || '').slice(e.offset, e.offset + e.length).toLowerCase();
        if (mention === `@${this._botUsername}`) return true;
      }
    }
    return false;
  }

  _parseForward(msg) {
    const fwd = msg.forward_origin;
    if (!fwd) return null;
    const result = { type: fwd.type, date: fwd.date ? new Date(fwd.date * 1000).toISOString() : null };
    switch (fwd.type) {
      case 'user':
        result.sender = this._formatUser(fwd.sender_user);
        break;
      case 'hidden_user':
        result.sender = fwd.sender_user_name || 'hidden';
        break;
      case 'chat':
        result.sender = fwd.sender_chat?.title || fwd.sender_chat?.username || `group:${fwd.sender_chat?.id}`;
        if (fwd.author_signature) result.signature = fwd.author_signature;
        break;
      case 'channel':
        result.sender = fwd.chat?.title || fwd.chat?.username || `channel:${fwd.chat?.id}`;
        if (fwd.author_signature) result.signature = fwd.author_signature;
        break;
    }
    return result;
  }

  _parseReply(msg) {
    const reply = msg.reply_to_message;
    if (!reply) {
      if (msg.quote?.text) {
        return { kind: 'quote', quoteText: msg.quote.text };
      }
      return null;
    }
    const result = {
      kind: msg.quote?.text ? 'quote' : 'reply',
      messageId: reply.message_id,
      sender: this._formatUser(reply.from),
      senderId: reply.from?.id,
      body: reply.text || reply.caption || (reply.photo ? '<media:photo>' : reply.document ? '<media:document>' : ''),
    };
    if (msg.quote?.text) result.quoteText = msg.quote.text;
    return result;
  }

  _parseLocation(msg) {
    if (msg.venue) {
      return {
        type: 'venue',
        latitude: msg.venue.location.latitude,
        longitude: msg.venue.location.longitude,
        title: msg.venue.title,
        address: msg.venue.address,
      };
    }
    if (msg.location) {
      return {
        type: msg.location.live_period ? 'live_location' : 'location',
        latitude: msg.location.latitude,
        longitude: msg.location.longitude,
      };
    }
    return null;
  }

  _formatUser(user) {
    if (!user) return 'unknown';
    const parts = [];
    if (user.first_name) parts.push(user.first_name);
    if (user.last_name) parts.push(user.last_name);
    const name = parts.join(' ') || String(user.id);
    return user.username ? `${name} (@${user.username})` : name;
  }
}
