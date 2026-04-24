import https from 'node:https';
import fs from 'node:fs';
import path from 'node:path';
import { log } from './log.js';

const RETRIABLE_CODES = new Set([
  'ECONNRESET', 'ECONNREFUSED', 'EPIPE', 'ETIMEDOUT',
  'ENETUNREACH', 'EHOSTUNREACH', 'ENOTFOUND', 'EAI_AGAIN',
]);

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function request(token, method, body, { retries = 2, timeoutMs = 60_000 } = {}) {
  const data = body ? JSON.stringify(body) : undefined;
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const result = await _doRequest(token, method, data, timeoutMs);
      return result;
    } catch (e) {
      if (e.retryAfter) {
        log.warn('rate limited, backing off', { method, retryAfter: e.retryAfter });
        await sleep(e.retryAfter * 1000);
        continue;
      }
      if (attempt < retries && RETRIABLE_CODES.has(e.code)) {
        log.warn('retriable error, retrying', { method, code: e.code, attempt });
        await sleep(1000 * (attempt + 1));
        continue;
      }
      throw e;
    }
  }
}

function _doRequest(token, method, data, timeoutMs) {
  return new Promise((resolve, reject) => {
    const opts = {
      hostname: 'api.telegram.org',
      path: `/bot${token}/${method}`,
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
    };
    const req = https.request(opts, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => {
        try {
          const json = JSON.parse(buf);
          if (!json.ok) {
            const err = new Error(`TG API ${method}: ${json.description || buf}`);
            err.errorCode = json.error_code;
            if (json.parameters?.retry_after || json.error_code === 429) {
              err.retryAfter = json.parameters?.retry_after || 5;
            }
            reject(err);
          } else {
            resolve(json.result);
          }
        } catch (e) {
          reject(new Error(`TG API ${method}: bad JSON: ${buf.slice(0, 200)}`));
        }
      });
    });
    req.on('error', (e) => { e.code = e.code || 'UNKNOWN'; reject(e); });
    req.setTimeout(timeoutMs, () => { req.destroy(new Error('timeout')); });
    if (data) req.write(data);
    req.end();
  });
}

export async function getUpdates(token, offset, timeout = 30) {
  return request(token, 'getUpdates', { offset, timeout, allowed_updates: ['message', 'edited_message'] }, { timeoutMs: (timeout + 10) * 1000, retries: 1 });
}

export async function sendMessage(token, chatId, text, opts = {}) {
  const body = { chat_id: chatId, text };
  if (opts.parseMode) body.parse_mode = opts.parseMode;
  if (opts.replyTo) body.reply_parameters = { message_id: opts.replyTo };
  if (opts.disableLinkPreview) body.link_preview_options = { is_disabled: true };
  if (opts.threadId && opts.threadId !== 1) body.message_thread_id = opts.threadId;
  if (opts.disableNotification) body.disable_notification = true;
  return request(token, 'sendMessage', body);
}

export async function sendChatAction(token, chatId, action = 'typing', opts = {}) {
  const body = { chat_id: chatId, action };
  if (opts.threadId && opts.threadId !== 1) body.message_thread_id = opts.threadId;
  return request(token, 'sendChatAction', body, { retries: 0 });
}

export async function setMessageReaction(token, chatId, messageId, emoji) {
  const reaction = emoji ? [{ type: 'emoji', emoji }] : [];
  return request(token, 'setMessageReaction', {
    chat_id: chatId, message_id: messageId, reaction,
  }, { retries: 0 });
}

export async function deleteMessage(token, chatId, messageId) {
  try {
    return await request(token, 'deleteMessage', { chat_id: chatId, message_id: messageId }, { retries: 0 });
  } catch { /* best-effort */ }
}

export async function editMessageText(token, chatId, messageId, text, opts = {}) {
  const body = { chat_id: chatId, message_id: messageId, text };
  if (opts.parseMode) body.parse_mode = opts.parseMode;
  if (opts.disableLinkPreview) body.link_preview_options = { is_disabled: true };
  return request(token, 'editMessageText', body);
}

export async function getFile(token, fileId) {
  return request(token, 'getFile', { file_id: fileId });
}

export async function downloadFile(token, filePath, destPath) {
  return new Promise((resolve, reject) => {
    const dir = path.dirname(destPath);
    if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
    const file = fs.createWriteStream(destPath);
    const url = `https://api.telegram.org/file/bot${token}/${filePath}`;
    https.get(url, (res) => {
      if (res.statusCode !== 200) {
        reject(new Error(`download ${filePath}: HTTP ${res.statusCode}`));
        return;
      }
      res.pipe(file);
      file.on('finish', () => { file.close(); resolve(destPath); });
    }).on('error', (e) => {
      fs.unlink(destPath, () => {});
      reject(e);
    });
  });
}

function _multipartUpload(token, method, fieldName, chatId, filePath, opts = {}) {
  return new Promise((resolve, reject) => {
    const boundary = '----TelegraphBoundary' + Date.now();
    const fileName = path.basename(filePath);
    const stat = fs.statSync(filePath);

    let formFields = `--${boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n${chatId}\r\n`;
    if (opts.replyTo) formFields += `--${boundary}\r\nContent-Disposition: form-data; name="reply_to_message_id"\r\n\r\n${opts.replyTo}\r\n`;
    if (opts.caption) formFields += `--${boundary}\r\nContent-Disposition: form-data; name="caption"\r\n\r\n${opts.caption}\r\n`;
    if (opts.parseMode) formFields += `--${boundary}\r\nContent-Disposition: form-data; name="parse_mode"\r\n\r\n${opts.parseMode}\r\n`;
    if (opts.threadId && opts.threadId !== 1) formFields += `--${boundary}\r\nContent-Disposition: form-data; name="message_thread_id"\r\n\r\n${opts.threadId}\r\n`;

    const contentType = opts.mimeType || 'application/octet-stream';
    formFields += `--${boundary}\r\nContent-Disposition: form-data; name="${fieldName}"; filename="${fileName}"\r\nContent-Type: ${contentType}\r\n\r\n`;

    const preamble = Buffer.from(formFields);
    const epilogue = Buffer.from(`\r\n--${boundary}--\r\n`);

    const reqOpts = {
      hostname: 'api.telegram.org',
      path: `/bot${token}/${method}`,
      method: 'POST',
      headers: {
        'Content-Type': `multipart/form-data; boundary=${boundary}`,
        'Content-Length': preamble.length + stat.size + epilogue.length,
      },
    };

    const req = https.request(reqOpts, (res) => {
      let buf = '';
      res.on('data', (c) => { buf += c; });
      res.on('end', () => {
        try {
          const json = JSON.parse(buf);
          json.ok ? resolve(json.result) : reject(new Error(json.description));
        } catch (e) { reject(e); }
      });
    });
    req.on('error', reject);
    req.setTimeout(120_000, () => { req.destroy(new Error('timeout')); });

    req.write(preamble);
    const stream = fs.createReadStream(filePath);
    stream.on('data', (chunk) => req.write(chunk));
    stream.on('end', () => { req.write(epilogue); req.end(); });
    stream.on('error', reject);
  });
}

export function sendDocument(token, chatId, filePath, opts = {}) {
  return _multipartUpload(token, 'sendDocument', 'document', chatId, filePath, opts);
}

export function sendPhoto(token, chatId, filePath, opts = {}) {
  return _multipartUpload(token, 'sendPhoto', 'photo', chatId, filePath, { mimeType: 'image/jpeg', ...opts });
}

export function sendVoice(token, chatId, filePath, opts = {}) {
  return _multipartUpload(token, 'sendVoice', 'voice', chatId, filePath, { mimeType: 'audio/ogg', ...opts });
}

export function sendVideo(token, chatId, filePath, opts = {}) {
  return _multipartUpload(token, 'sendVideo', 'video', chatId, filePath, { mimeType: 'video/mp4', ...opts });
}

export function sendAudio(token, chatId, filePath, opts = {}) {
  return _multipartUpload(token, 'sendAudio', 'audio', chatId, filePath, opts);
}
