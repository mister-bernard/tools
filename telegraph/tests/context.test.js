import { describe, it, before, after } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { ContextTracker } from '../src/context.js';

describe('ContextTracker', () => {
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'telegraph-test-'));
  const ctxFile = path.join(tmpDir, 'shared-context.md');

  before(() => {
    fs.writeFileSync(ctxFile, '# Shared Context\n\nuser approved deploy v2.1');
  });

  after(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
  });

  it('returns delta on first call for a session', () => {
    const tracker = new ContextTracker(ctxFile);
    const delta = tracker.getDelta('session-primary');
    assert.ok(delta);
    assert.ok(delta.includes('user approved deploy'));
  });

  it('returns null after markSeen', () => {
    const tracker = new ContextTracker(ctxFile);
    tracker.getDelta('session-primary');
    tracker.markSeen('session-primary');
    const delta = tracker.getDelta('session-primary');
    assert.equal(delta, null);
  });

  it('returns delta after file change', () => {
    const tracker = new ContextTracker(ctxFile);
    tracker.getDelta('session-primary');
    tracker.markSeen('session-primary');

    fs.writeFileSync(ctxFile, '# Shared Context\n\nG approved deploy v2.2');
    tracker._reload();

    const delta = tracker.getDelta('session-primary');
    assert.ok(delta);
    assert.ok(delta.includes('v2.2'));
  });

  it('tracks sessions independently', () => {
    const tracker = new ContextTracker(ctxFile);
    tracker.getDelta('session-primary');
    tracker.markSeen('session-primary');

    const delta = tracker.getDelta('session-secondary');
    assert.ok(delta, 'session-secondary should still see delta');
  });

  it('handles missing file gracefully', () => {
    const tracker = new ContextTracker('/nonexistent/file.md');
    const delta = tracker.getDelta('session-primary');
    assert.equal(delta, null);
  });
});
