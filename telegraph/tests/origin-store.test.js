import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { OriginStore } from '../src/origin-store.js';

const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'origin-store-test-'));

describe('OriginStore', () => {
  after(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('register → lookup round-trip', () => {
    const file = path.join(tmpDir, 'a.jsonl');
    const s = new OriginStore({ file });
    s.load();

    s.register({
      chatId: 10000001,
      msgId: 25010,
      origin: { type: 'tmux', target: 'gg:1.3' },
    });

    const found = s.lookup(10000001, 25010);
    assert.deepEqual(found, { type: 'tmux', target: 'gg:1.3' });
  });

  it('lookup returns null for unknown key', () => {
    const file = path.join(tmpDir, 'b.jsonl');
    const s = new OriginStore({ file });
    s.load();
    assert.equal(s.lookup(1, 1), null);
  });

  it('jsonl survives restart (load rebuilds map)', () => {
    const file = path.join(tmpDir, 'c.jsonl');
    const s1 = new OriginStore({ file });
    s1.load();
    s1.register({ chatId: 1, msgId: 100, origin: { type: 'script', label: 'x' } });
    s1.register({ chatId: 1, msgId: 101, origin: { type: 'bridge', session: 'session-primary' } });

    const s2 = new OriginStore({ file });
    s2.load();
    assert.equal(s2.size(), 2);
    assert.deepEqual(s2.lookup(1, 100), { type: 'script', label: 'x' });
    assert.deepEqual(s2.lookup(1, 101), { type: 'bridge', session: 'session-primary' });
  });

  it('maxRecent evicts oldest from memory', () => {
    const file = path.join(tmpDir, 'd.jsonl');
    const s = new OriginStore({ file, maxRecent: 3 });
    s.load();
    for (let i = 1; i <= 5; i++) {
      s.register({ chatId: 1, msgId: i, origin: { type: 'script', label: `${i}` } });
    }
    assert.equal(s.size(), 3);
    assert.equal(s.lookup(1, 1), null);
    assert.equal(s.lookup(1, 2), null);
    assert.deepEqual(s.lookup(1, 5)?.label, '5');
  });

  it('rejects malformed origin', () => {
    const file = path.join(tmpDir, 'e.jsonl');
    const s = new OriginStore({ file });
    s.load();
    assert.throws(() => s.register({ chatId: 1, msgId: 1, origin: null }));
    assert.throws(() => s.register({ chatId: 1, msgId: 1, origin: { notype: true } }));
  });

  it('skips malformed lines when loading', () => {
    const file = path.join(tmpDir, 'f.jsonl');
    fs.writeFileSync(file, 'not json\n{"chat_id":1,"msg_id":2,"origin":{"type":"tmux"}}\n\n');
    const s = new OriginStore({ file });
    s.load();
    assert.equal(s.size(), 1);
    assert.deepEqual(s.lookup(1, 2), { type: 'tmux' });
  });
});
