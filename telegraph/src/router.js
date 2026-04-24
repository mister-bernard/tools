import { log } from './log.js';

export class Router {
  constructor(bindings) {
    this._bindings = bindings;
  }

  resolve(accountId, chatType, chatId, userId) {
    for (const b of this._bindings) {
      if (b.account !== accountId) continue;
      if (b.chat !== chatType) continue;
      const peerId = chatType === 'dm' ? String(userId) : String(chatId);
      if (b.peer !== '*' && b.peer !== peerId) continue;
      log.debug('route matched', { accountId, chatType, chatId, userId, session: b.session });
      return { session: b.session };
    }
    log.warn('no route matched', { accountId, chatType, chatId, userId });
    return null;
  }
}
