import { spawn } from 'node:child_process';
import { log } from './log.js';

// Dispatches an inbound message to the agent identified by its origin
// metadata, rather than the static binding table. Called by handleMessage
// when replyContext.messageId resolves in the OriginStore.
//
// Delivery contract: returns { ok: true } on success,
// { ok: false, reason: '<code>', detail? } on fallback-needed.

export class OriginDispatcher {
  constructor({ bridgeClient }) {
    this._bridge = bridgeClient;
  }

  // Main entry. Returns { ok, reason, text? }.
  //   ok=true  → dispatch succeeded; caller should NOT use fallback routing.
  //   ok=false → caller should fall back to static routing.
  //
  // `msg` is the raw poller msg (for tmux, we build our own injection from it
  //   rather than reusing the buildPrompt output — tmux panes have their own
  //   state and don't need bridge's session-spawn context).
  // `prompt` is the buildPrompt output (used by bridge origins).
  async dispatch({ origin, prompt, user, promptMeta, msg }) {
    switch (origin.type) {
      case 'bridge':
        return this._dispatchCcBridge(origin, prompt, user);
      case 'tmux':
        return this._dispatchTmux(origin, msg, promptMeta);
      case 'script':
      case 'cron':
        return { ok: false, reason: 'no-inbox', detail: `origin.type=${origin.type} has no inbox` };
      default:
        return { ok: false, reason: 'unknown-type', detail: `unknown origin.type=${origin.type}` };
    }
  }

  async _dispatchCcBridge(origin, prompt, user) {
    try {
      const result = await this._bridge.complete(origin.session, prompt, { user });
      return { ok: true, text: result.text };
    } catch (e) {
      log.error('bridge dispatch failed', { session: origin.session, error: e.message });
      return { ok: false, reason: 'bridge-error', detail: e.message };
    }
  }

  // tmux send-keys delivery.
  //
  // Verifies the pane still exists, then injects the message as a prompt
  // turn. The CLI's assistant sees it as a new user input.
  //
  // NOTE: This is fire-and-forget — there's no synchronous reply. The CLI
  // assistant will respond in its own pane when it gets to it. the user will see
  // the reply in the terminal, not back on Telegram.
  async _dispatchTmux(origin, msg, promptMeta) {
    if (!origin.target) {
      return { ok: false, reason: 'bad-origin', detail: 'tmux origin missing target' };
    }

    const socket = origin.socket || null;

    const exists = await this._tmuxPaneExists(origin.target, socket);
    if (!exists) {
      return { ok: false, reason: 'pane-gone', detail: `tmux pane ${origin.target} not found${socket ? ` (socket ${socket})` : ''}` };
    }

    const injected = this._buildTmuxInjection(msg, promptMeta);

    try {
      await this._tmuxSendKeys(origin.target, injected, socket);
      log.info('tmux dispatch ok', { target: origin.target, socket });
      return { ok: true, text: null, delivered: 'tmux' };
    } catch (e) {
      log.error('tmux dispatch failed', { target: origin.target, socket, error: e.message });
      return { ok: false, reason: 'tmux-error', detail: e.message };
    }
  }

  // Build the text that gets pasted into the tmux pane. Distinct from the
  // bridge prompt because:
  //   - the pane has its own conversation state (no session-spawn context)
  //   - the receiving Claude needs to clearly see quote vs new text
  //   - local-format noise (peer/chat metadata) is redundant here
  _buildTmuxInjection(msg, promptMeta) {
    const from = promptMeta?.from || msg?.username || 'user';
    const via = promptMeta?.channel || 'telegram';
    const msgId = msg?.messageId;
    const parts = [];

    parts.push(`[${via} reply ← ${from}${msgId ? `, msg #${msgId}` : ''}]`);
    parts.push('');

    const rc = msg?.replyContext;
    if (rc) {
      const kindLabel = rc.kind === 'quote' ? 'Quoting' : 'Replying to';
      const who = rc.sender || 'previous message';
      const refId = rc.messageId ? ` #${rc.messageId}` : '';
      parts.push(`--- ${kindLabel} ${who}${refId}: ---`);
      const quoted = (rc.quoteText || rc.body || '').trim();
      if (quoted) {
        const trimmed = quoted.length > 500 ? quoted.slice(0, 500) + '…' : quoted;
        for (const line of trimmed.split('\n')) parts.push(`> ${line}`);
      } else {
        parts.push('> (no text — media or empty)');
      }
      parts.push('');
    }

    if (msg?.forward) {
      parts.push(`[Forwarded from ${msg.forward.sender}]`);
      parts.push('');
    }

    if (msg?.location) {
      const loc = msg.location;
      if (loc.type === 'venue') {
        parts.push(`[Venue: ${loc.title}, ${loc.address} (${loc.latitude}, ${loc.longitude})]`);
      } else {
        parts.push(`[Location: ${loc.latitude}, ${loc.longitude}]`);
      }
      parts.push('');
    }

    parts.push(`--- ${from} wrote: ---`);
    const text = (msg?.text || '').trim();
    parts.push(text || '(no text)');

    if (msg?.mediaPaths?.length) {
      parts.push('');
      for (let i = 0; i < msg.mediaPaths.length; i++) {
        const mt = msg.mediaTypes[i];
        const label = { photo: 'Photo', document: 'Document', voice: 'Voice message', audio: 'Audio', video: 'Video', video_note: 'Video note' }[mt] || 'File';
        parts.push(`[${label} attached: ${msg.mediaPaths[i]} — use Read to view]`);
      }
    }

    return parts.join('\n');
  }

  _tmuxArgs(socket, extra) {
    return socket ? ['-S', socket, ...extra] : extra;
  }

  _tmuxPaneExists(target, socket) {
    return new Promise((resolve) => {
      const p = spawn('tmux', this._tmuxArgs(socket, ['display-message', '-p', '-t', target, '#{pane_id}']), {
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      let out = '';
      p.stdout.on('data', (c) => { out += c; });
      p.on('close', (code) => resolve(code === 0 && out.trim().startsWith('%')));
      p.on('error', () => resolve(false));
    });
  }

  // Writes text as a single load-buffer → paste-buffer operation. This is
  // safer than piping through send-keys literal, because newlines and quotes
  // in the content won't be re-interpreted as keybindings.
  _tmuxSendKeys(target, text, socket) {
    const args = (extra) => this._tmuxArgs(socket, extra);
    return new Promise((resolve, reject) => {
      const load = spawn('tmux', args(['load-buffer', '-b', 'telegraph-origin', '-']), {
        stdio: ['pipe', 'ignore', 'pipe'],
      });
      let err = '';
      load.stderr.on('data', (c) => { err += c; });
      load.on('error', reject);
      load.on('close', (code) => {
        if (code !== 0) return reject(new Error(`tmux load-buffer failed: ${err}`));
        // -p: bracketed-paste mode — target TUI (Claude Code) sees the whole
        //     paste as a single atomic input event, preserving embedded \n
        //     instead of treating them as Enter keypresses.
        // -d: delete the buffer after paste.
        const paste = spawn('tmux', args(['paste-buffer', '-p', '-d', '-b', 'telegraph-origin', '-t', target]), {
          stdio: ['ignore', 'ignore', 'pipe'],
        });
        let err2 = '';
        paste.stderr.on('data', (c) => { err2 += c; });
        paste.on('error', reject);
        paste.on('close', (code2) => {
          if (code2 !== 0) return reject(new Error(`tmux paste-buffer failed: ${err2}`));
          const enter = spawn('tmux', args(['send-keys', '-t', target, 'Enter']), { stdio: 'ignore' });
          enter.on('error', reject);
          enter.on('close', (code3) => {
            if (code3 !== 0) return reject(new Error('tmux send-keys Enter failed'));
            resolve();
          });
        });
      });
      load.stdin.write(text);
      load.stdin.end();
    });
  }
}
