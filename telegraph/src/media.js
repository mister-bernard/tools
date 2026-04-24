import path from 'node:path';
import * as tg from './tg-api.js';
import { log } from './log.js';

const MEDIA_DIR = '/tmp/telegraph-media';

async function download(token, fileId, fallbackName) {
  try {
    const file = await tg.getFile(token, fileId);
    const ext = path.extname(file.file_path) || '';
    const dest = path.join(MEDIA_DIR, fallbackName + ext);
    await tg.downloadFile(token, file.file_path, dest);
    log.info('downloaded media', { dest });
    return dest;
  } catch (e) {
    log.error('media download failed', { error: e.message });
    return null;
  }
}

export async function downloadPhoto(token, photos) {
  if (!photos?.length) return null;
  const best = photos[photos.length - 1];
  return download(token, best.file_id, `photo-${best.file_unique_id}`);
}

export async function downloadMedia(token, media, type) {
  if (!media) return null;
  const name = media.file_name || `${type}-${media.file_unique_id}`;
  return download(token, media.file_id, name);
}
