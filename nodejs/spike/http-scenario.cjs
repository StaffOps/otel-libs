'use strict';
// One HTTP round trip: client GET -> server handler that creates a child span
// on the active context (check 4). The app passes in the http functions it
// imported, so each app exercises its own import style.

/**
 * @param {typeof import('node:http').createServer} createServer
 * @param {typeof import('node:http').get} get
 * @param {import('@opentelemetry/api').Tracer} tracer
 */
async function run(createServer, get, tracer) {
  const server = createServer((req, res) => {
    tracer.startActiveSpan('child-in-handler', (span) => {
      span.end();
      res.end('ok');
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  await new Promise((resolve, reject) => {
    get(`http://127.0.0.1:${port}/spike`, (res) => {
      res.resume();
      res.on('end', resolve);
    }).on('error', reject);
  });
  await new Promise((resolve) => server.close(resolve));
}

module.exports = { run };
