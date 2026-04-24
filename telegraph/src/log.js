const LEVEL = { debug: 0, info: 1, warn: 2, error: 3 };
const threshold = LEVEL[process.env.LOG_LEVEL || 'info'] ?? LEVEL.info;

function emit(level, msg, extra) {
  if (LEVEL[level] < threshold) return;
  const line = { ts: new Date().toISOString(), level, msg, ...extra };
  process.stderr.write(JSON.stringify(line) + '\n');
}

export const log = {
  debug: (msg, extra) => emit('debug', msg, extra),
  info:  (msg, extra) => emit('info', msg, extra),
  warn:  (msg, extra) => emit('warn', msg, extra),
  error: (msg, extra) => emit('error', msg, extra),
};
