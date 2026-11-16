// Redis Streams queue with manual W3C propagation (specs/demo-environment
// DE-D5, DE-D6): span names, attributes, carrier inject/extract and the
// CONSUMER span options (one link to the producer and the producer as parent).
'use strict';

const { ROOT_CONTEXT, SpanKind, context: contextApi, propagation, trace } = require('@opentelemetry/api');

/** Consumer group of every language's stream. */
const CONSUMER_GROUP = 'demo-workers';

/** The stream of a language (DE-D4). */
function streamKey(lang) {
  return `demo:${lang}:orders`;
}

/** The api's quote cache key of a customer (DE-D4). */
function quoteKey(lang, customer) {
  return `demo:${lang}:quote:${customer}`;
}

function baseAttributes(operation, stream, server) {
  return {
    'messaging.system': 'redis',
    'messaging.operation.name': operation,
    'messaging.operation.type': operation,
    'messaging.destination.name': stream,
    'server.address': server.host,
    'server.port': server.port,
  };
}

/** PRODUCER span name and options; `messaging.message.id` is set after XADD returns. */
function producerSpan(stream, server) {
  return {
    name: `send ${stream}`,
    options: { kind: SpanKind.PRODUCER, attributes: baseAttributes('send', stream, server) },
  };
}

/** The propagation fields of `ctx` (traceparent, tracestate, baggage) as a string map. */
function injectCarrier(ctx = contextApi.active()) {
  const carrier = {};
  propagation.inject(ctx, carrier);
  const fields = {};
  for (const [key, value] of Object.entries(carrier)) {
    if (typeof value === 'string') fields[key] = value;
  }
  return fields;
}

/** The context carried by a message's fields, extracted on top of the root context. */
function extractContext(fields) {
  const carrier = {};
  for (const [key, value] of Object.entries(fields ?? {})) {
    if (typeof value === 'string') carrier[key] = value;
  }
  return propagation.extract(ROOT_CONTEXT, carrier);
}

/**
 * CONSUMER span name, options and parent context of one message: one link to
 * the producer's context and that context as parent (single-message
 * processing, semconv v1.43.0 messaging spans). A message without a valid
 * context gets no link and a root parent.
 */
function consumerSpan(stream, server, messageId, fields) {
  const parent = extractContext(fields);
  const producer = trace.getSpanContext(parent);
  const valid = producer !== undefined && trace.isSpanContextValid(producer);
  return {
    name: `process ${stream}`,
    parent: valid ? parent : ROOT_CONTEXT,
    options: {
      kind: SpanKind.CONSUMER,
      links: valid ? [{ context: producer }] : [],
      attributes: {
        ...baseAttributes('process', stream, server),
        'messaging.message.id': messageId,
        'messaging.consumer.group.name': CONSUMER_GROUP,
      },
    },
  };
}

module.exports = { CONSUMER_GROUP, consumerSpan, extractContext, injectCarrier, producerSpan, quoteKey, streamKey };
