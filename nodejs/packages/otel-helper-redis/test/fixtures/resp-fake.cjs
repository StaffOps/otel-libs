'use strict';
// A small in-process RESP server (task B10b, spec JS8 "a RESP fake"): enough
// of Redis for node-redis 6 (RESP3 through HELLO 3) and ioredis 6 (RESP2) —
// HELLO, AUTH (NOAUTH / WRONGPASS), PING,
// SELECT, INFO, CLIENT, SET/GET/DEL/INCR/LPUSH (WRONGTYPE), MULTI/EXEC,
// SUBSCRIBE/PUBLISH, QUIT, and Redis 7's unknown-command error that quotes
// the arguments. Every received command is kept in `commands` (tests assert
// what the client sent, never what a span recorded).
const net = require('node:net');

const CRLF = '\r\n';
const simple = (s) => `+${s}${CRLF}`;
const error = (s) => `-${s}${CRLF}`;
const integer = (n) => `:${n}${CRLF}`;
const bulkString = (s) => `$${Buffer.byteLength(s)}${CRLF}${s}${CRLF}`;
const array = (items) => `*${items.length}${CRLF}${items.join('')}`;
/** Out-of-band pub/sub frames: a RESP3 push, a RESP2 array. */
const push = (proto, items) => `${proto === 3 ? '>' : '*'}${items.length}${CRLF}${items.join('')}`;

/** Parses one RESP array of bulk strings from `buf` at `at`; `undefined` when incomplete. */
function parse(buf, at) {
  const line = (from) => {
    const end = buf.indexOf(CRLF, from);
    return end < 0 ? undefined : [buf.toString('utf8', from, end), end + 2];
  };
  const head = line(at);
  if (head === undefined) return undefined;
  let [text, next] = head;
  if (text[0] !== '*') return { args: text.split(' ').filter(Boolean), next }; // inline command
  const count = Number(text.slice(1));
  const args = [];
  for (let i = 0; i < count; i++) {
    const len = line(next);
    if (len === undefined) return undefined;
    const size = Number(len[0].slice(1));
    if (buf.length < len[1] + size + 2) return undefined;
    args.push(buf.toString('utf8', len[1], len[1] + size));
    next = len[1] + size + 2;
  }
  return { args, next };
}

/**
 * Starts the fake on 127.0.0.1 (a free port). `password`: AUTH required.
 * Resolves `{ port, url, commands, close() }`.
 */
async function startRespFake({ password } = {}) {
  const store = new Map();
  const subscribers = new Map(); // channel -> Map<socket, connection state>
  const commands = [];
  const sockets = new Set();

  function execute(state, socket, args) {
    const name = String(args[0] ?? '').toUpperCase();
    const rest = args.slice(1);
    const bulk = (v) => (v !== null ? bulkString(v) : state.proto === 3 ? `_${CRLF}` : `$-1${CRLF}`);
    if (name === 'HELLO') {
      const proto = rest[0] === undefined ? state.proto : Number(rest[0]);
      if (proto !== 2 && proto !== 3) return error('NOPROTO unsupported protocol version');
      const at = rest.findIndex((a) => String(a).toUpperCase() === 'AUTH');
      if (at >= 0) {
        if (password === undefined || rest[at + 2] !== password) return error('WRONGPASS invalid username-password pair or user is disabled.');
        state.authed = true;
      }
      if (password !== undefined && !state.authed) return error('NOAUTH HELLO must be called with the client already authenticated, otherwise the HELLO <proto> AUTH <user> <pass> option can be used to authenticate the client and select the RESP protocol version at the same time');
      state.proto = proto;
      const info = [['server', 'redis'], ['version', '7.4.0'], ['proto', proto], ['id', 1], ['mode', 'standalone'], ['role', 'master']];
      const values = info.flatMap(([k, v]) => [bulkString(k), typeof v === 'number' ? integer(v) : bulkString(v)]);
      return proto === 3 ? `%${info.length}${CRLF}${values.join('')}` : array(values);
    }
    if (name === 'AUTH') {
      const given = rest[rest.length - 1];
      if (password === undefined) return error('ERR AUTH <password> called without any password configured for the default user. Are you sure your configuration is correct?');
      if (given !== password) return error('WRONGPASS invalid username-password pair or user is disabled.');
      state.authed = true;
      return simple('OK');
    }
    if (password !== undefined && !state.authed && name !== 'QUIT') return error('NOAUTH Authentication required.');
    if (state.queue !== undefined && !['EXEC', 'DISCARD', 'MULTI'].includes(name)) {
      state.queue.push(args);
      return simple('QUEUED');
    }
    switch (name) {
      case 'PING':
        return state.subscribed > 0 && state.proto === 2 ? array([bulk('pong'), bulk('')]) : simple('PONG');
      case 'SELECT':
      case 'CLIENT':
        return simple('OK');
      case 'INFO':
        return bulk('# Server\r\nredis_version:7.4.0\r\nloading:0\r\n');
      case 'QUIT':
        setImmediate(() => socket.end());
        return simple('OK');
      case 'SET':
        store.set(rest[0], rest[1]);
        return simple('OK');
      case 'GET': {
        const v = store.get(rest[0]);
        if (Array.isArray(v)) return error('WRONGTYPE Operation against a key holding the wrong kind of value');
        return bulk(v ?? null);
      }
      case 'DEL':
        return integer(rest.filter((k) => store.delete(k)).length);
      case 'INCR': {
        const v = Number(store.get(rest[0]) ?? '0');
        if (!Number.isInteger(v)) return error('ERR value is not an integer or out of range');
        store.set(rest[0], String(v + 1));
        return integer(v + 1);
      }
      case 'LPUSH': {
        const v = store.get(rest[0]) ?? [];
        if (!Array.isArray(v)) return error('WRONGTYPE Operation against a key holding the wrong kind of value');
        v.unshift(...rest.slice(1));
        store.set(rest[0], v);
        return integer(v.length);
      }
      case 'EVAL': {
        // redis.error_reply(...) as a real server answers it (script_lua.c: the first word is the code).
        if ((rest[0] ?? '').includes("ARGV[1] .. ' bad'")) return error(`${rest[2]} bad`);
        const m = /error_reply\('([^']*)'\)/.exec(rest[0] ?? '');
        return error(m ? m[1] : 'ERR Error compiling script');
      }
      case 'MULTI':
        state.queue = [];
        return simple('OK');
      case 'DISCARD':
        state.queue = undefined;
        return simple('OK');
      case 'EXEC': {
        const queued = state.queue ?? [];
        state.queue = undefined;
        return array(queued.map((q) => execute(state, socket, q)));
      }
      case 'SUBSCRIBE':
        return rest
          .map((ch) => {
            if (!subscribers.has(ch)) subscribers.set(ch, new Map());
            subscribers.get(ch).set(socket, state);
            state.subscribed += 1;
            return push(state.proto, [bulk('subscribe'), bulk(ch), integer(state.subscribed)]);
          })
          .join('');
      case 'UNSUBSCRIBE':
        return (rest.length > 0 ? rest : [...subscribers.keys()])
          .map((ch) => {
            subscribers.get(ch)?.delete(socket);
            state.subscribed = Math.max(0, state.subscribed - 1);
            return push(state.proto, [bulk('unsubscribe'), bulk(ch), integer(state.subscribed)]);
          })
          .join('');
      case 'PUBLISH': {
        const subs = [...(subscribers.get(rest[0]) ?? [])];
        for (const [s, st] of subs) s.write(push(st.proto, [bulkString('message'), bulkString(rest[0]), bulkString(rest[1])]));
        return integer(subs.length);
      }
      default:
        return error(`ERR unknown command '${args[0]}', with args beginning with: ${rest.map((a) => `'${a}' `).join('')}`);
    }
  }

  const server = net.createServer((socket) => {
    sockets.add(socket);
    const state = { authed: false, queue: undefined, subscribed: 0, proto: 2 };
    let buf = Buffer.alloc(0);
    socket.on('data', (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      let at = 0;
      for (;;) {
        const cmd = parse(buf, at);
        if (cmd === undefined) break;
        at = cmd.next;
        commands.push(cmd.args);
        socket.write(execute(state, socket, cmd.args));
      }
      buf = buf.subarray(at);
    });
    socket.on('error', () => undefined);
    socket.on('close', () => {
      sockets.delete(socket);
      for (const subs of subscribers.values()) subs.delete(socket);
    });
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const { port } = server.address();
  const auth = password === undefined ? '' : `default:${encodeURIComponent(password)}@`;
  return {
    port,
    url: `redis://${auth}127.0.0.1:${port}`,
    commands,
    close: () =>
      new Promise((resolve) => {
        for (const s of sockets) s.destroy();
        server.close(() => resolve());
      }),
  };
}

module.exports = { startRespFake };
