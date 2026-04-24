import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Poller } from './poller.js';
import { SignalPoller } from './signal-poller.js';
import { Router } from './router.js';
import { BridgeClient } from './bridge-client.js';
import { Delivery } from './delivery.js';
import { ContextTracker } from './context.js';
import { CommandHandler, isPassthroughCommand } from './commands.js';
import { OriginStore } from './origin-store.js';
import { OriginDispatcher } from './origin-dispatcher.js';
import * as tgApi from './tg-api.js';
import { log } from './log.js';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(__dirname, '..');

function loadConfig() {
  const raw = fs.readFileSync(path.join(ROOT, 'telegraph.json'), 'utf8');
  let text = raw;
  for (const [key, val] of Object.entries(process.env)) {
    text = text.replaceAll(`\${${key}}`, val);
  }
  return JSON.parse(text);
}

const config = loadConfig();
const router = new Router(config.bindings);
const bridge = new BridgeClient(config.bridge);
const delivery = new Delivery({ parseMode: config.delivery?.parseMode || 'HTML' });
const context = new ContextTracker(config.context?.sharedContextFile || null);

const originStoreEnabled = config.originStore?.enabled !== false;
const originStore = new OriginStore({
  file: path.join(ROOT, 'state', 'origins.jsonl'),
  maxRecent: config.originStore?.maxRecent || 5000,
});
const originDispatcher = new OriginDispatcher({ bridgeClient: bridge });
if (originStoreEnabled) originStore.load();

const commands = new CommandHandler({ bridge, config, contextTracker: context, router });
commands.startedAt = Date.now();

const stats = { started: Date.now(), messagesIn: 0, messagesOut: 0, errors: 0 };

let signalPoller = null;

function buildPrompt(msg, contextDelta) {
  const channel = msg.accountId === 'signal' ? 'signal' : 'telegram';
  const parts = [];

  parts.push(`[peer=${channel}:${msg.userId}] [chat=${msg.chatType}:${msg.chatId}] [from=${msg.username}] [msg_id=${msg.messageId}]`);
  if (msg.edited) parts.push('[edited message]');

  if (contextDelta) {
    parts.push('');
    parts.push('[Context update since your last turn]');
    parts.push(contextDelta);
    parts.push('---');
  }

  if (msg.forward) {
    parts.push('');
    parts.push(`[Forwarded from ${msg.forward.sender}${msg.forward.signature ? ` (${msg.forward.signature})` : ''}${msg.forward.date ? `, ${msg.forward.date}` : ''}]`);
  }

  if (msg.replyContext) {
    parts.push('');
    const rc = msg.replyContext;
    if (rc.kind === 'quote') {
      parts.push(`[Quote-reply to ${rc.sender || 'msg'} #${rc.messageId || '?'}]`);
      if (rc.quoteText) parts.push(`> ${rc.quoteText.split('\n').join('\n> ')}`);
    } else {
      parts.push(`[Reply to ${rc.sender || 'msg'} #${rc.messageId || '?'}]`);
      if (rc.body) parts.push(`> ${rc.body.slice(0, 200)}`);
    }
  }

  if (msg.location) {
    parts.push('');
    const loc = msg.location;
    if (loc.type === 'venue') {
      parts.push(`[Venue: ${loc.title}, ${loc.address} (${loc.latitude}, ${loc.longitude})]`);
    } else {
      parts.push(`[Location: ${loc.latitude}, ${loc.longitude}${loc.type === 'live_location' ? ' (live)' : ''}]`);
    }
  }

  parts.push('');
  if (msg.text) parts.push(msg.text);

  for (let i = 0; i < msg.mediaPaths.length; i++) {
    const mt = msg.mediaTypes[i];
    const mp = msg.mediaPaths[i];
    const label = { photo: 'Photo', document: 'Document', voice: 'Voice message', audio: 'Audio', video: 'Video', video_note: 'Video note' }[mt] || 'File';
    parts.push(`[${label} attached: ${mp} -- use Read to view]`);
  }
  for (let i = msg.mediaPaths.length; i < msg.mediaTypes.length; i++) {
    if (msg.mediaTypes[i] === 'sticker') parts.push('[Sticker received]');
  }

  return parts.join('\n');
}

async function deliverTelegram(token, msg, text) {
  return delivery.send(token, msg.chatId, text, {
    replyTo: msg.messageId,
    threadId: msg.threadId,
  });
}

async function deliverSignal(msg, text) {
  if (!signalPoller) return [];
  if (delivery.shouldSkip(text)) return [];

  const filePaths = delivery.extractFilePaths(text);

  if (msg.chatType === 'group') {
    await signalPoller.sendGroupMessage(msg.chatId, text);
  } else {
    await signalPoller.send(msg.userId, text);
  }

  for (const fp of filePaths) {
    try {
      if (msg.chatType === 'group') {
        await signalPoller.sendGroupMessage(msg.chatId, '', fp);
      } else {
        await signalPoller.sendAttachment(msg.userId, fp);
      }
    } catch (e) {
      log.error('signal file send failed', { file: fp, error: e.message });
    }
  }

  return [1];
}

async function handleMessage(msg) {
  stats.messagesIn++;
  const isSignal = msg.accountId === 'signal';

  if (msg.text && commands.isCommand(msg.text)) {
    const token = !isSignal ? config.accounts[msg.accountId]?.botToken : null;
    const deliverCmd = async (text) => {
      if (isSignal) {
        await deliverSignal(msg, text);
      } else if (token) {
        await deliverTelegram(token, msg, text);
      }
    };
    const handled = await commands.handle(msg, deliverCmd);
    if (handled) return;
  }

  if (msg.text && isPassthroughCommand(msg.text)) {
    const cmd = msg.text.split(/\s/)[0].slice(1).toLowerCase();
    msg.text = `Please run your /${cmd} command and share the result.`;
  }

  // Origin-routing: if this is a reply to a tracked outbound message,
  // route to the agent that originally sent it instead of the static table.
  let originHit = null;
  if (originStoreEnabled && msg.replyContext?.messageId) {
    originHit = originStore.lookup(msg.chatId, msg.replyContext.messageId);
    if (originHit) {
      log.info('origin-routing hit', {
        chat: msg.chatId,
        replyTo: msg.replyContext.messageId,
        originType: originHit.type,
        originTarget: originHit.target || originHit.session || originHit.unit,
      });
    }
  }

  const route = router.resolve(msg.accountId, msg.chatType, msg.chatId, msg.userId);
  if (!route && !originHit) {
    log.warn('unrouted message, dropping', { chat: msg.chatId, user: msg.userId, account: msg.accountId });
    return;
  }

  const session = route?.session;

  let typingInterval = null;
  if (!isSignal) {
    const token = config.accounts[msg.accountId]?.botToken;
    if (!token) { log.error('no bot token', { account: msg.accountId }); return; }
    typingInterval = setInterval(() => {
      tgApi.sendChatAction(token, msg.chatId, 'typing', { threadId: msg.threadId }).catch(() => {});
    }, 4000);
    tgApi.sendChatAction(token, msg.chatId, 'typing', { threadId: msg.threadId }).catch(() => {});
  }

  const channel = isSignal ? 'signal' : 'telegram';
  const contextDelta = context.getDelta(session);
  let prompt = buildPrompt(msg, contextDelta);

  try {
    // Origin-routed delivery first, fall back to static session on miss.
    if (originHit) {
      const r = await originDispatcher.dispatch({
        origin: originHit,
        prompt,
        msg,
        user: `${channel}:${msg.userId}`,
        promptMeta: { from: msg.username, channel },
      });
      if (r.ok) {
        if (typingInterval) clearInterval(typingInterval);
        if (r.text) {
          // bridge origin returned text to deliver back to Telegram.
          let sentIds;
          if (isSignal) sentIds = await deliverSignal(msg, r.text);
          else {
            const token = config.accounts[msg.accountId].botToken;
            sentIds = await deliverTelegram(token, msg, r.text);
          }
          stats.messagesOut += sentIds.length;
          log.info('origin-routed response delivered', { origin: originHit.type, chunks: sentIds.length });
        } else {
          // tmux origin: injected into pane, no synchronous reply expected.
          log.info('origin-routed to tmux pane', { origin: originHit.type, target: originHit.target });
          if (!isSignal) {
            const token = config.accounts[msg.accountId].botToken;
            await deliverTelegram(token, msg, `⚡ routed to pane \`${originHit.target}\``).catch(() => {});
          }
        }
        return;
      }
      log.warn('origin-routing fallback', { reason: r.reason, detail: r.detail });
      // Decorate the prompt so the static-route session knows the replied-to
      // message wasn't its own — it was a script/cron/etc. originated msg.
      // Without this, session-primary sees a "got it" reply with no context for
      // what was acknowledged.
      const orig = originHit;
      const desc = orig.type === 'script' || orig.type === 'cron'
        ? `script/${orig.label || 'unknown'} (a non-conversational webhook or cron job — no live agent owns this thread)`
        : `${orig.type}${orig.session ? `/${orig.session}` : ''}${orig.target ? `/${orig.target}` : ''} (origin agent unreachable: ${r.reason})`;
      prompt = `[Origin context — fallback to static route]\nThe message the user replied to (msg #${msg.replyContext?.messageId}) was originally sent by: ${desc}.\nReason for fallback: ${r.reason}${r.detail ? ` (${r.detail})` : ''}.\nYou are receiving this because there is no live inbox for the original sender. Treat this as an out-of-band reply — answer if you can be useful, otherwise acknowledge briefly.\n---\n\n${prompt}`;
    }

    if (!session) {
      log.warn('no static route after origin miss, dropping', { chat: msg.chatId });
      if (typingInterval) clearInterval(typingInterval);
      return;
    }

    log.info('dispatching to bridge', { channel, session, promptLen: prompt.length });

    const result = await bridge.complete(session, prompt, {
      user: `${channel}:${msg.userId}`,
    });

    if (typingInterval) clearInterval(typingInterval);
    context.markSeen(session);

    let sentIds;
    if (isSignal) {
      sentIds = await deliverSignal(msg, result.text);
    } else {
      const token = config.accounts[msg.accountId].botToken;
      sentIds = await deliverTelegram(token, msg, result.text);
    }

    stats.messagesOut += sentIds.length;
    log.info('response delivered', { channel, session, chatId: msg.chatId, chunks: sentIds.length });
  } catch (e) {
    if (typingInterval) clearInterval(typingInterval);
    stats.errors++;
    log.error('dispatch failed', { channel, session, error: e.message });
  }
}

const PORT = parseInt(process.env.TELEGRAPH_PORT || '18903', 10);
const BIND = process.env.TELEGRAPH_BIND || '127.0.0.1';

const server = http.createServer((req, res) => {
  if (req.url === '/healthz' || req.url === '/health') {
    const body = JSON.stringify({
      status: 'ok',
      uptime_sec: Math.floor((Date.now() - stats.started) / 1000),
      accounts: [...Object.keys(config.accounts), ...(config.signal ? ['signal'] : [])],
      bindings: config.bindings.length,
      origins: originStoreEnabled ? originStore.size() : null,
      ...stats,
    });
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(body);
    return;
  }

  // POST /origin — register an outbound msg_id → origin mapping.
  // Body: {chat_id, msg_id, origin: {type, ...}}
  if (req.url === '/origin' && req.method === 'POST') {
    if (!originStoreEnabled) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'origin-store disabled' }));
      return;
    }
    let buf = '';
    req.on('data', (c) => { buf += c; if (buf.length > 10_000) req.destroy(); });
    req.on('end', () => {
      try {
        const b = JSON.parse(buf);
        if (b.chat_id == null || b.msg_id == null || !b.origin?.type) {
          res.writeHead(400, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'need chat_id, msg_id, origin.type' }));
          return;
        }
        const rec = originStore.register({ chatId: b.chat_id, msgId: b.msg_id, origin: b.origin });
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ok: true, recorded: rec }));
      } catch (e) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: e.message }));
      }
    });
    return;
  }

  // GET /origin/:chat_id/:msg_id — lookup (for debugging)
  const lookupMatch = req.url.match(/^\/origin\/(-?\d+)\/(\d+)$/);
  if (lookupMatch && req.method === 'GET') {
    const found = originStore.lookup(parseInt(lookupMatch[1], 10), parseInt(lookupMatch[2], 10));
    res.writeHead(found ? 200 : 404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify(found || { error: 'not found' }));
    return;
  }

  res.writeHead(404);
  res.end('not found');
});

const pollers = [];

async function main() {
  log.info('telegraph starting', {
    port: PORT,
    accounts: Object.keys(config.accounts),
    signal: !!config.signal,
    bindings: config.bindings.length,
  });

  context.start();

  server.listen(PORT, BIND, () => {
    log.info('health endpoint ready', { bind: `${BIND}:${PORT}` });
  });

  for (const [accountId, acct] of Object.entries(config.accounts)) {
    if (!acct.botToken) {
      log.warn('skipping account, no token', { account: accountId });
      continue;
    }
    const stateDir = path.join(ROOT, 'state');
    if (!fs.existsSync(stateDir)) fs.mkdirSync(stateDir, { recursive: true });

    const poller = new Poller({
      accountId,
      token: acct.botToken,
      botUsername: acct.botUsername || '',
      stateDir,
      onMessage: handleMessage,
      pollTimeout: acct.pollTimeout || 30,
      ackReaction: acct.ackReaction || null,
      requireMention: acct.requireMention || false,
    });
    pollers.push(poller);
    poller.start();
  }

  if (config.signal?.rpcUrl) {
    signalPoller = new SignalPoller({
      account: config.signal.account,
      rpcUrl: config.signal.rpcUrl,
      onMessage: handleMessage,
      pollIntervalMs: config.signal.pollIntervalMs || 2000,
    });
    pollers.push(signalPoller);
    signalPoller.start();
  }
}

function shutdown(signal) {
  log.info('shutting down', { signal });
  for (const p of pollers) p.stop();
  context.stop();
  server.close();
  setTimeout(() => process.exit(0), 2000);
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

main().catch((e) => {
  log.error('fatal', { error: e.message, stack: e.stack });
  process.exit(1);
});
