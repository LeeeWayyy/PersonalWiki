import assert from 'node:assert/strict';
import test from 'node:test';

import { streamJob } from '../../src/scripts/backend-client.js';

globalThis.localStorage = { getItem: () => '' };

function response(...chunks) {
  const encoder = new TextEncoder();
  return new Response(new ReadableStream({
    start(controller) {
      chunks.forEach((chunk) => controller.enqueue(encoder.encode(chunk)));
      controller.close();
    },
  }));
}

test('streamJob resolves only for a successful terminal status', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => response('data: working\n\n', 'event: done\ndata: done\n\n'));
  const lines = [];

  await streamJob('job-1', (line) => lines.push(line));

  assert.deepEqual(lines, ['working', 'done: done']);
});

for (const status of ['error', 'blocked', 'canceled']) {
  test(`streamJob rejects the ${status} terminal status`, async (t) => {
    t.mock.method(globalThis, 'fetch', async () => response(`event: done\ndata: ${status}\n\n`));
    await assert.rejects(streamJob('job-1', () => {}), new RegExp(`job ${status}`));
  });
}

test('streamJob rejects a stream that closes before its terminal event', async (t) => {
  t.mock.method(globalThis, 'fetch', async () => response('data: working\n\n'));
  await assert.rejects(streamJob('job-1', () => {}), /closed before terminal status/);
});
