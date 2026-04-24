import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { Delivery } from '../src/delivery.js';

describe('Delivery', () => {
  const d = new Delivery();

  describe('shouldSkip', () => {
    it('skips empty text', () => assert.equal(d.shouldSkip(''), true));
    it('skips whitespace', () => assert.equal(d.shouldSkip('  '), true));
    it('skips NO_REPLY', () => assert.equal(d.shouldSkip('NO_REPLY'), true));
    it('skips HEARTBEAT_OK', () => assert.equal(d.shouldSkip('HEARTBEAT_OK'), true));
    it('does not skip normal text', () => assert.equal(d.shouldSkip('hello'), false));
  });

  describe('escapeHtml', () => {
    it('escapes ampersand', () => assert.equal(d.escapeHtml('a&b'), 'a&amp;b'));
    it('escapes angle brackets', () => assert.equal(d.escapeHtml('<div>'), '&lt;div&gt;'));
    it('handles mixed', () => assert.equal(d.escapeHtml('a < b & c > d'), 'a &lt; b &amp; c &gt; d'));
  });

  describe('mdToHtml', () => {
    it('converts bold', () => assert.equal(d.mdToHtml('**bold**'), '<b>bold</b>'));
    it('converts inline code and escapes contents', () => {
      assert.equal(d.mdToHtml('use `a<b>c`'), 'use <code>a&lt;b&gt;c</code>');
    });
    it('converts code blocks and escapes contents', () => {
      const md = '```js\nconst x = a<b;\n```';
      const html = d.mdToHtml(md);
      assert.ok(html.includes('<pre><code>const x = a&lt;b;\n</code></pre>'));
    });
    it('escapes HTML in normal text', () => {
      assert.equal(d.mdToHtml('if a < b & c > d'), 'if a &lt; b &amp; c &gt; d');
    });
    it('converts links', () => {
      assert.equal(d.mdToHtml('[click](http://x.com)'), '<a href="http://x.com">click</a>');
    });
    it('converts strikethrough', () => assert.equal(d.mdToHtml('~~old~~'), '<s>old</s>'));
    it('handles mixed markdown and HTML escaping', () => {
      const md = '**a<b>** and `c&d`';
      const html = d.mdToHtml(md);
      assert.ok(html.includes('<b>a&lt;b&gt;</b>'));
      assert.ok(html.includes('<code>c&amp;d</code>'));
    });
  });

  describe('chunk', () => {
    it('returns single chunk for short text', () => {
      const chunks = d.chunk('hello');
      assert.equal(chunks.length, 1);
    });

    it('splits at paragraph break', () => {
      const text = 'a'.repeat(3500) + '\n\n' + 'b'.repeat(3500);
      const chunks = d.chunk(text);
      assert.equal(chunks.length, 2);
    });

    it('handles very long text', () => {
      const text = Array(5).fill('x'.repeat(3500)).join('\n\n');
      const chunks = d.chunk(text);
      assert.ok(chunks.length >= 4);
      for (const c of chunks) {
        assert.ok(c.length <= 4000, `chunk too long: ${c.length}`);
      }
    });

    it('does not split inside HTML entity', () => {
      const text = 'a'.repeat(3995) + '&amp;' + 'b'.repeat(100);
      const chunks = d.chunk(text);
      assert.ok(!chunks[0].endsWith('&am'));
      assert.ok(!chunks[0].endsWith('&'));
    });
  });

  describe('extractFilePaths', () => {
    it('extracts nothing from plain text', () => {
      assert.deepEqual(d.extractFilePaths('just text'), []);
    });
  });
});
