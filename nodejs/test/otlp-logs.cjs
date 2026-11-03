'use strict';
// Test helper shared by the -pino and -winston suites (B10d): a local OTLP/HTTP
// receiver and a decoder of ExportLogsServiceRequest protobuf bodies (the
// opentelemetry-proto field numbers of logs/v1, common/v1 and resource/v1),
// so the tests assert decoded records instead of matching raw bytes.
const http = require('node:http');

/** The wire fields of one protobuf message: `[{ no, wire, value }]` (value: bigint or Buffer). */
function fields(buf) {
  const out = [];
  let i = 0;
  const varint = () => {
    let result = 0n;
    let shift = 0n;
    for (;;) {
      if (i >= buf.length) throw new Error('truncated varint');
      const b = buf[i++];
      result |= BigInt(b & 0x7f) << shift;
      if ((b & 0x80) === 0) return result;
      shift += 7n;
    }
  };
  while (i < buf.length) {
    const key = Number(varint());
    const no = key >>> 3;
    const wire = key & 7;
    if (wire === 0) out.push({ no, wire, value: varint() });
    else if (wire === 1) {
      out.push({ no, wire, value: buf.subarray(i, i + 8) });
      i += 8;
    } else if (wire === 2) {
      const len = Number(varint());
      out.push({ no, wire, value: buf.subarray(i, i + len) });
      i += len;
    } else if (wire === 5) {
      out.push({ no, wire, value: buf.subarray(i, i + 4) });
      i += 4;
    } else throw new Error(`unsupported wire type ${wire}`);
  }
  return out;
}

const all = (buf, no) => fields(buf).filter((f) => f.no === no).map((f) => f.value);
const one = (buf, no) => all(buf, no)[0];
const text = (v) => (v === undefined ? undefined : v.toString('utf8'));

/** common.v1.AnyValue -> a JS value (int as number, bytes as Buffer, kvlist as object). */
function anyValue(buf) {
  for (const f of fields(buf)) {
    switch (f.no) {
      case 1: return text(f.value);
      case 2: return f.value !== 0n;
      case 3: return Number(BigInt.asIntN(64, f.value));
      case 4: return f.value.readDoubleLE(0);
      case 5: return all(f.value, 1).map(anyValue);
      case 6: return keyValues(all(f.value, 1));
      case 7: return Buffer.from(f.value);
      default: break;
    }
  }
  return null;
}

/** repeated common.v1.KeyValue -> object (defined properties: a key such as __proto__ stays data). */
function keyValues(list) {
  const out = {};
  for (const kv of list) {
    const value = one(kv, 2);
    Object.defineProperty(out, text(one(kv, 1)), { value: value === undefined ? null : anyValue(value), enumerable: true, writable: true, configurable: true });
  }
  return out;
}

/** ExportLogsServiceRequest -> flat records `{ scope, severityNumber, severityText, body, attributes, traceId, spanId, timeUnixNano, resource }`. */
function decodeLogs(body) {
  const records = [];
  for (const rl of all(body, 1)) {
    const resourceBuf = one(rl, 1);
    const resource = resourceBuf === undefined ? {} : keyValues(all(resourceBuf, 1));
    for (const sl of all(rl, 2)) {
      const scopeBuf = one(sl, 1);
      const scope = scopeBuf === undefined ? '' : text(one(scopeBuf, 1)) ?? '';
      for (const lr of all(sl, 2)) {
        const bodyBuf = one(lr, 5);
        const time = one(lr, 1);
        records.push({
          scope,
          resource,
          timeUnixNano: time === undefined ? 0n : time.readBigUInt64LE(0),
          severityNumber: Number(one(lr, 2) ?? 0n),
          severityText: text(one(lr, 3)) ?? '',
          body: bodyBuf === undefined ? undefined : anyValue(bodyBuf),
          attributes: keyValues(all(lr, 6)),
          traceId: one(lr, 9) === undefined ? '' : one(lr, 9).toString('hex'),
          spanId: one(lr, 10) === undefined ? '' : one(lr, 10).toString('hex'),
        });
      }
    }
  }
  return records;
}

/** An OTLP/HTTP receiver on 127.0.0.1 (ephemeral port) answering 200 to every request. */
function otlpReceiver() {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      requests.push({ url: req.url, body: Buffer.concat(chunks) });
      res.writeHead(200, { 'content-type': 'application/x-protobuf' });
      res.end();
    });
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      resolve({
        endpoint: `http://127.0.0.1:${port}`,
        requests,
        /** Every log record received so far, decoded. */
        logs: () => requests.filter((r) => r.url === '/v1/logs').flatMap((r) => decodeLogs(r.body)),
        close: () => new Promise((done) => server.close(() => done())),
      });
    });
  });
}

/** Runs fn with process.stdout.write captured; returns the written lines parsed as JSON. */
function captureStdout(fn) {
  const chunks = [];
  const original = process.stdout.write;
  process.stdout.write = (chunk) => {
    chunks.push(String(chunk));
    return true;
  };
  try {
    fn();
  } finally {
    process.stdout.write = original;
  }
  return chunks.join('').split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

module.exports = { decodeLogs, otlpReceiver, captureStdout };
