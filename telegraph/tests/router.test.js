import { describe, it } from 'node:test';
import assert from 'node:assert/strict';
import { Router } from '../src/router.js';

const bindings = [
  { account: 'secondary-account', chat: 'dm',    peer: '10000001',    session: 'session-secondary' },
  { account: 'default', chat: 'dm',    peer: '10000001',    session: 'session-primary' },
  { account: 'default', chat: 'group', peer: '-100000001', session: 'session-default' },
  { account: 'default', chat: 'group', peer: '*',           session: 'session-default' },
  { account: 'default', chat: 'dm',    peer: '*',           session: 'session-default' },
];

describe('Router', () => {
  const router = new Router(bindings);

  it('matches primary DM on default account to session-primary', () => {
    const r = router.resolve('default', 'dm', '10000001', '10000001');
    assert.equal(r.session, 'session-primary');
  });

  it('matches primary DM on secondary-account to session-secondary', () => {
    const r = router.resolve('secondary-account', 'dm', '10000001', '10000001');
    assert.equal(r.session, 'session-secondary');
  });

  it('matches specific group to session-default', () => {
    const r = router.resolve('default', 'group', '-100000001', '12345');
    assert.equal(r.session, 'session-default');
  });

  it('matches wildcard group', () => {
    const r = router.resolve('default', 'group', '-9999999999', '12345');
    assert.equal(r.session, 'session-default');
  });

  it('matches wildcard DM for unknown users', () => {
    const r = router.resolve('default', 'dm', '9999', '9999');
    assert.equal(r.session, 'session-default');
  });

  it('returns null for unknown account', () => {
    const r = router.resolve('nonexistent', 'dm', '10000001', '10000001');
    assert.equal(r, null);
  });

  it('first match wins (secondary-account before default)', () => {
    const r = router.resolve('secondary-account', 'dm', '10000001', '10000001');
    assert.equal(r.session, 'session-secondary');
  });
});
