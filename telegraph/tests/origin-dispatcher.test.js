import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { OriginDispatcher } from '../src/origin-dispatcher.js';

function mockBridge(impl) {
  return {
    complete: impl || (async () => ({ text: 'mocked reply' })),
  };
}

describe('OriginDispatcher', () => {
  it('bridge origin calls bridge.complete with session id', async () => {
    let captured = null;
    const bridge = mockBridge(async (session, prompt, opts) => {
      captured = { session, prompt, opts };
      return { text: 'resp' };
    });
    const d = new OriginDispatcher({ bridgeClient: bridge });
    const r = await d.dispatch({
      origin: { type: 'bridge', session: 'session-primary', endpoint: 'http://x' },
      prompt: 'hello',
      user: 'telegram:42',
    });
    assert.equal(r.ok, true);
    assert.equal(r.text, 'resp');
    assert.equal(captured.session, 'session-primary');
    assert.equal(captured.prompt, 'hello');
  });

  it('bridge origin returns ok:false when bridge fails', async () => {
    const bridge = mockBridge(async () => { throw new Error('upstream down'); });
    const d = new OriginDispatcher({ bridgeClient: bridge });
    const r = await d.dispatch({
      origin: { type: 'bridge', session: 'x' },
      prompt: 'p',
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'bridge-error');
  });

  it('script origin returns no-inbox fallback', async () => {
    const d = new OriginDispatcher({ bridgeClient: mockBridge() });
    const r = await d.dispatch({
      origin: { type: 'script', label: 'book-factory' },
      prompt: 'p',
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'no-inbox');
  });

  it('unknown origin type returns unknown-type', async () => {
    const d = new OriginDispatcher({ bridgeClient: mockBridge() });
    const r = await d.dispatch({
      origin: { type: 'carrier-pigeon' },
      prompt: 'p',
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'unknown-type');
  });

  it('tmux origin with missing target returns bad-origin', async () => {
    const d = new OriginDispatcher({ bridgeClient: mockBridge() });
    const r = await d.dispatch({
      origin: { type: 'tmux' },
      prompt: 'p',
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'bad-origin');
  });

  it('tmux origin with bogus target returns pane-gone', async () => {
    const d = new OriginDispatcher({ bridgeClient: mockBridge() });
    const r = await d.dispatch({
      origin: { type: 'tmux', target: 'nonexistent-session:999.999' },
      prompt: 'p',
    });
    assert.equal(r.ok, false);
    assert.equal(r.reason, 'pane-gone');
  });

  describe('_buildTmuxInjection', () => {
    const d = new OriginDispatcher({ bridgeClient: mockBridge() });

    it('plain new message — no reply context', () => {
      const out = d._buildTmuxInjection(
        { username: 'testuser', messageId: 42, text: 'hi there', mediaPaths: [], mediaTypes: [] },
        { channel: 'telegram', from: 'testuser' }
      );
      assert.match(out, /\[telegram reply ← testuser, msg #42\]/);
      assert.match(out, /--- testuser wrote: ---\nhi there$/);
      assert.doesNotMatch(out, /Replying to|Quoting/);
    });

    it('normal reply — distinct quote block', () => {
      const out = d._buildTmuxInjection(
        {
          username: 'testuser',
          messageId: 101,
          text: 'testing a normal reply',
          replyContext: { kind: 'reply', messageId: 25258, sender: 'TestSender', body: 'Socket fix deployed.' },
          mediaPaths: [], mediaTypes: [],
        },
        { channel: 'telegram', from: 'testuser' }
      );
      assert.match(out, /--- Replying to TestSender #25258: ---/);
      assert.match(out, /> Socket fix deployed\./);
      assert.match(out, /--- testuser wrote: ---\ntesting a normal reply/);
      // quote block comes BEFORE new text block
      assert.ok(out.indexOf('Replying to') < out.indexOf('testuser wrote'));
    });

    it('quote-reply uses "Quoting" verb', () => {
      const out = d._buildTmuxInjection(
        {
          username: 'testuser',
          messageId: 102,
          text: 'nice',
          replyContext: { kind: 'quote', messageId: 25258, sender: 'TestSender', quoteText: '.' },
          mediaPaths: [], mediaTypes: [],
        },
        { channel: 'telegram', from: 'testuser' }
      );
      assert.match(out, /--- Quoting TestSender #25258: ---/);
      assert.match(out, /> \./);
    });

    it('truncates long quoted text', () => {
      const longBody = 'x'.repeat(800);
      const out = d._buildTmuxInjection(
        {
          username: 'testuser', messageId: 103, text: 'hmm',
          replyContext: { kind: 'reply', body: longBody },
          mediaPaths: [], mediaTypes: [],
        },
        { from: 'testuser' }
      );
      assert.ok(out.includes('…'));
      assert.ok(out.length < 800);
    });

    it('no context-delta/peer metadata noise', () => {
      const out = d._buildTmuxInjection(
        { username: 'testuser', messageId: 1, text: 'x', mediaPaths: [], mediaTypes: [] },
        { from: 'testuser', channel: 'telegram' }
      );
      assert.doesNotMatch(out, /\[peer=/);
      assert.doesNotMatch(out, /Context update/);
      assert.doesNotMatch(out, /\[chat=/);
    });

    it('empty text with media', () => {
      const out = d._buildTmuxInjection(
        {
          username: 'testuser', messageId: 1, text: '',
          mediaPaths: ['/tmp/p.jpg'], mediaTypes: ['photo'],
        },
        { from: 'testuser' }
      );
      assert.match(out, /\(no text\)/);
      assert.match(out, /\[Photo attached: \/tmp\/p\.jpg — use Read to view\]/);
    });
  });
});
