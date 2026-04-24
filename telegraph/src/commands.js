import fs from 'node:fs';
import { execSync } from 'node:child_process';
import { log } from './log.js';

const BRIDGE_ENV = '/opt/bridge/.env';

export class CommandHandler {
  constructor({ bridge, config, contextTracker, router }) {
    this._bridge = bridge;
    this._config = config;
    this._context = contextTracker;
    this._router = router;
  }

  isCommand(text) {
    if (!text || !text.startsWith('/')) return false;
    const cmd = text.split(/\s/)[0].slice(1).toLowerCase().replace(/@\w+$/, '');
    return COMMANDS.has(cmd);
  }

  async handle(msg, deliverFn) {
    const parts = msg.text.trim().split(/\s+/);
    const cmd = parts[0].slice(1).toLowerCase().replace(/@\w+$/, '');
    const args = parts.slice(1);
    const handler = COMMANDS.get(cmd);
    if (!handler) return false;

    try {
      const reply = await handler.call(this, msg, args);
      if (reply) await deliverFn(reply);
    } catch (e) {
      log.error('command failed', { cmd, error: e.message });
      await deliverFn(`Command /${cmd} failed: ${e.message}`);
    }
    return true;
  }

  async _cmdHelp() {
    return [
      '<b>Telegraph Commands</b>',
      '',
      '/help — this message',
      '/health — system status',
      '/ping — alive check',
      '/sessions — list active bridge sessions',
      '/model [name] — show or switch model (opus, sonnet, haiku)',
      '/effort [level] — thinking effort (low, medium, high, max)',
      '/context — show shared-context.md',
      '/clear — kill your session (next msg spawns fresh)',
      '/compact — ask Claude to compact context',
      '/memory — ask Claude to show memory',
      '/cost — ask Claude for cost summary',
      '',
      '<b>Cost guide</b>: opus ~$15/$75 per MTok, sonnet ~$3/$15, haiku ~$0.80/$4.',
      'Effort controls thinking tokens — low effort on sonnet is the cheapest useful config.',
    ].join('\n');
  }

  async _cmdPing() {
    const up = Math.floor((Date.now() - this._startedAt) / 1000);
    return `pong (${up}s uptime)`;
  }

  async _cmdHealth() {
    let bridgeHealth = 'unreachable';
    try {
      const h = await this._bridge.health();
      const sessionCount = h.sessions?.active ?? Object.keys(h.sessions || {}).length;
      bridgeHealth = `ok, ${sessionCount} sessions`;
    } catch (e) {
      bridgeHealth = e.message;
    }

    const { models, effort } = readBridgeEnv();
    const signalStatus = this._config.signal?.rpcUrl ? 'active' : 'off';
    const accounts = Object.keys(this._config.accounts).join(', ');

    const lines = [
      '<b>Telegraph</b>: running',
      `<b>bridge</b>: ${bridgeHealth}`,
      `<b>Accounts</b>: ${accounts}`,
      `<b>Signal</b>: ${signalStatus}`,
      `<b>Bindings</b>: ${this._config.bindings.length}`,
    ];
    if (Object.keys(models).length) {
      lines.push('', '<b>Model overrides</b>:');
      for (const [sid, m] of Object.entries(models)) lines.push(`  ${sid}: ${m}`);
    }
    if (Object.keys(effort).length) {
      lines.push('', '<b>Effort overrides</b>:');
      for (const [sid, args] of Object.entries(effort)) {
        const effortFlag = args.find((_, i, a) => i > 0 && a[i - 1] === '--effort') || 'default';
        lines.push(`  ${sid}: ${effortFlag}`);
      }
    }
    return lines.join('\n');
  }

  async _cmdSessions() {
    try {
      const h = await this._bridge.health();
      const sessions = h.sessions || {};
      if (typeof sessions === 'object' && !Array.isArray(sessions)) {
        const lines = ['<b>Active sessions</b>:'];
        for (const [id, info] of Object.entries(sessions)) {
          const status = info.status || info.state || 'unknown';
          const model = info.model || '';
          lines.push(`• ${id}: ${status}${model ? ` (${model})` : ''}`);
        }
        return lines.join('\n') || 'No active sessions.';
      }
      return `Sessions: ${JSON.stringify(sessions)}`;
    } catch (e) {
      return `Could not reach bridge: ${e.message}`;
    }
  }

  async _cmdModel(msg, args) {
    const route = this._resolveSession(msg);
    if (!route) return 'No session found for this chat.';
    const session = route.session;

    const { models, effort } = readBridgeEnv();

    if (args.length === 0) {
      const current = models[session] || '(global default)';
      return `<code>${session}</code> model: <b>${current}</b>`;
    }

    const newModel = args[0].toLowerCase();
    const valid = ['opus', 'sonnet', 'haiku'];
    if (!valid.includes(newModel)) {
      return `Invalid model. Choose: ${valid.join(', ')}`;
    }

    models[session] = newModel;
    writeBridgeEnvField('BRIDGE_SESSION_MODELS', JSON.stringify(models));
    restartBridge();
    log.info('model switched', { session, model: newModel });

    return `Model for <code>${session}</code> → <b>${newModel}</b>. bridge restarted.`;
  }

  async _cmdEffort(msg, args) {
    const route = this._resolveSession(msg);
    if (!route) return 'No session found for this chat.';
    const session = route.session;

    const { effort } = readBridgeEnv();

    if (args.length === 0) {
      const current = effort[session];
      if (!current) return `<code>${session}</code> effort: <b>default</b> (no override)`;
      const level = current.find((_, i, a) => i > 0 && a[i - 1] === '--effort') || 'default';
      return `<code>${session}</code> effort: <b>${level}</b>`;
    }

    const level = args[0].toLowerCase();
    const valid = ['low', 'medium', 'high', 'xhigh', 'max'];
    if (valid.includes(level)) {
      const existing = effort[session] || [];
      const cleaned = [];
      for (let i = 0; i < existing.length; i++) {
        if (existing[i] === '--effort') { i++; continue; }
        cleaned.push(existing[i]);
      }
      cleaned.push('--effort', level);
      effort[session] = cleaned;
    } else if (level === 'default' || level === 'off' || level === 'reset') {
      const existing = effort[session] || [];
      const cleaned = [];
      for (let i = 0; i < existing.length; i++) {
        if (existing[i] === '--effort') { i++; continue; }
        cleaned.push(existing[i]);
      }
      if (cleaned.length === 0) {
        delete effort[session];
      } else {
        effort[session] = cleaned;
      }
    } else {
      return `Invalid effort. Choose: ${valid.join(', ')}, or "default" to reset.`;
    }

    const filtered = Object.fromEntries(
      Object.entries(effort).filter(([, v]) => v && v.length > 0)
    );
    writeBridgeEnvField('BRIDGE_SESSION_EXTRA_ARGS', JSON.stringify(filtered));
    restartBridge();
    log.info('effort switched', { session, level });

    return `Effort for <code>${session}</code> → <b>${level}</b>. bridge restarted.`;
  }

  async _cmdContext() {
    const ctxFile = this._config.context?.sharedContextFile;
    if (!ctxFile) return 'No shared-context file configured.';
    try {
      const content = fs.readFileSync(ctxFile, 'utf8');
      if (!content.trim()) return 'shared-context.md is empty.';
      const truncated = content.length > 3000 ? content.slice(0, 3000) + '\n...(truncated)' : content;
      return `<b>shared-context.md</b>\n<pre>${escapeHtml(truncated)}</pre>`;
    } catch {
      return 'Could not read shared-context.md.';
    }
  }

  async _cmdClear(msg) {
    const route = this._resolveSession(msg);
    if (!route) return 'No session found for this chat.';
    return `To clear <code>${route.session}</code>, send: "Please clear your conversation and start fresh."`;
  }

  _resolveSession(msg) {
    if (!this._router) return null;
    return this._router.resolve(msg.accountId, msg.chatType, msg.chatId, msg.userId);
  }

  set startedAt(ts) { this._startedAt = ts; }
}

function readBridgeEnv() {
  const env = fs.readFileSync(BRIDGE_ENV, 'utf8');
  let models = {};
  let effort = {};

  const mMatch = env.match(/^BRIDGE_SESSION_MODELS=(.*)$/m);
  if (mMatch) try { models = JSON.parse(mMatch[1]); } catch { /* */ }

  const eMatch = env.match(/^BRIDGE_SESSION_EXTRA_ARGS=(.*)$/m);
  if (eMatch) try { effort = JSON.parse(eMatch[1]); } catch { /* */ }

  return { models, effort };
}

function writeBridgeEnvField(key, value) {
  let env = fs.readFileSync(BRIDGE_ENV, 'utf8');
  const re = new RegExp(`^${key}=.*$`, 'm');
  if (re.test(env)) {
    env = env.replace(re, `${key}=${value}`);
  } else {
    env = env.trimEnd() + `\n${key}=${value}\n`;
  }
  fs.writeFileSync(BRIDGE_ENV, env);
}

function restartBridge() {
  try {
    execSync('systemctl --user restart bridge', { timeout: 10_000 });
    log.info('bridge restarted after config change');
  } catch (e) {
    log.error('bridge restart failed', { error: e.message });
    throw new Error('bridge restart failed: ' + e.message);
  }
}

const COMMANDS = new Map([
  ['help', CommandHandler.prototype._cmdHelp],
  ['health', CommandHandler.prototype._cmdHealth],
  ['ping', CommandHandler.prototype._cmdPing],
  ['sessions', CommandHandler.prototype._cmdSessions],
  ['model', CommandHandler.prototype._cmdModel],
  ['effort', CommandHandler.prototype._cmdEffort],
  ['context', CommandHandler.prototype._cmdContext],
  ['clear', CommandHandler.prototype._cmdClear],
]);

const PASSTHROUGH_COMMANDS = new Set([
  'compact', 'memory', 'cost', 'review',
]);

export function isPassthroughCommand(text) {
  if (!text || !text.startsWith('/')) return false;
  const cmd = text.split(/\s/)[0].slice(1).toLowerCase().replace(/@\w+$/, '');
  return PASSTHROUGH_COMMANDS.has(cmd);
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
