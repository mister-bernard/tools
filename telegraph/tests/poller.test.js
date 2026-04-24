import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { Poller } from '../src/poller.js';

describe('Poller message parsing', () => {
  const poller = new Poller({
    accountId: 'test',
    token: 'fake',
    botUsername: 'testbot',
    stateDir: '/tmp',
    onMessage: () => {},
  });

  describe('_expandTextLinks', () => {
    it('expands text_link entities to markdown', () => {
      const text = 'Click here for details';
      const entities = [{ type: 'text_link', offset: 6, length: 4, url: 'https://example.com' }];
      const result = poller._expandTextLinks(text, entities);
      assert.equal(result, 'Click [here](https://example.com) for details');
    });

    it('handles no entities', () => {
      assert.equal(poller._expandTextLinks('hello', []), 'hello');
    });

    it('handles multiple text_links in reverse order', () => {
      const text = 'Visit A and B now';
      const entities = [
        { type: 'text_link', offset: 6, length: 1, url: 'https://a.com' },
        { type: 'text_link', offset: 12, length: 1, url: 'https://b.com' },
      ];
      const result = poller._expandTextLinks(text, entities);
      assert.ok(result.includes('[A](https://a.com)'));
      assert.ok(result.includes('[B](https://b.com)'));
    });
  });

  describe('_isMentioned', () => {
    it('detects @botusername in text', () => {
      const msg = { text: 'hey @testbot what up', entities: [] };
      assert.equal(poller._isMentioned(msg), true);
    });

    it('detects mention entity', () => {
      const msg = { text: '@testbot hello', entities: [{ type: 'mention', offset: 0, length: 8 }] };
      assert.equal(poller._isMentioned(msg), true);
    });

    it('returns false when not mentioned', () => {
      const msg = { text: 'hello world', entities: [] };
      assert.equal(poller._isMentioned(msg), false);
    });

    it('is case-insensitive', () => {
      const msg = { text: 'hey @TestBot', entities: [] };
      assert.equal(poller._isMentioned(msg), true);
    });
  });

  describe('_parseForward', () => {
    it('parses user forward', () => {
      const msg = { forward_origin: { type: 'user', sender_user: { first_name: 'John', username: 'john123', id: 1 } } };
      const result = poller._parseForward(msg);
      assert.equal(result.type, 'user');
      assert.ok(result.sender.includes('John'));
      assert.ok(result.sender.includes('@john123'));
    });

    it('parses hidden_user forward', () => {
      const msg = { forward_origin: { type: 'hidden_user', sender_user_name: 'Anonymous' } };
      const result = poller._parseForward(msg);
      assert.equal(result.sender, 'Anonymous');
    });

    it('parses channel forward with signature', () => {
      const msg = { forward_origin: { type: 'channel', chat: { title: 'News', id: 1 }, author_signature: 'Editor' } };
      const result = poller._parseForward(msg);
      assert.equal(result.sender, 'News');
      assert.equal(result.signature, 'Editor');
    });

    it('returns null for non-forwarded', () => {
      assert.equal(poller._parseForward({}), null);
    });
  });

  describe('_parseReply', () => {
    it('parses simple reply', () => {
      const msg = { reply_to_message: { message_id: 42, from: { first_name: 'Jane', id: 2 }, text: 'original text' } };
      const result = poller._parseReply(msg);
      assert.equal(result.kind, 'reply');
      assert.equal(result.messageId, 42);
      assert.ok(result.body.includes('original text'));
    });

    it('parses quote-reply', () => {
      const msg = {
        reply_to_message: { message_id: 42, from: { first_name: 'Jane', id: 2 }, text: 'long text' },
        quote: { text: 'quoted part' },
      };
      const result = poller._parseReply(msg);
      assert.equal(result.kind, 'quote');
      assert.equal(result.quoteText, 'quoted part');
    });

    it('parses standalone quote without reply_to_message', () => {
      const msg = { quote: { text: 'standalone quote' } };
      const result = poller._parseReply(msg);
      assert.equal(result.kind, 'quote');
      assert.equal(result.quoteText, 'standalone quote');
    });

    it('returns null for non-reply', () => {
      assert.equal(poller._parseReply({}), null);
    });
  });

  describe('_parseLocation', () => {
    it('parses venue', () => {
      const msg = { venue: { location: { latitude: 47.5, longitude: 11.3 }, title: 'Cafe', address: '123 Main St' } };
      const result = poller._parseLocation(msg);
      assert.equal(result.type, 'venue');
      assert.equal(result.title, 'Cafe');
    });

    it('parses pin location', () => {
      const msg = { location: { latitude: 47.5, longitude: 11.3 } };
      const result = poller._parseLocation(msg);
      assert.equal(result.type, 'location');
    });

    it('parses live location', () => {
      const msg = { location: { latitude: 47.5, longitude: 11.3, live_period: 3600 } };
      const result = poller._parseLocation(msg);
      assert.equal(result.type, 'live_location');
    });

    it('returns null for non-location', () => {
      assert.equal(poller._parseLocation({}), null);
    });
  });

  describe('_formatUser', () => {
    it('formats user with username', () => {
      const result = poller._formatUser({ first_name: 'John', last_name: 'Doe', username: 'johnd', id: 1 });
      assert.equal(result, 'John Doe (@johnd)');
    });

    it('formats user without username', () => {
      const result = poller._formatUser({ first_name: 'John', id: 1 });
      assert.equal(result, 'John');
    });

    it('handles null user', () => {
      assert.equal(poller._formatUser(null), 'unknown');
    });
  });
});
