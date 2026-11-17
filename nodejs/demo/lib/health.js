// grpc.health.v1.Health for the backend (DE-D7 r3.4: every backend serves
// it). The service definition is the well-known health protocol, built from a
// JSON descriptor with @grpc/proto-loader (no extra package, no .proto file);
// the helper produces no span for these calls (HOW-TO §3).
'use strict';

const SERVING_STATUS = Object.freeze({ UNKNOWN: 0, SERVING: 1, NOT_SERVING: 2, SERVICE_UNKNOWN: 3 });

const DESCRIPTOR = {
  nested: {
    grpc: { nested: { health: { nested: { v1: { nested: {
      HealthCheckRequest: { fields: { service: { type: 'string', id: 1 } } },
      HealthCheckResponse: {
        fields: { status: { type: 'ServingStatus', id: 1 } },
        nested: { ServingStatus: { values: { ...SERVING_STATUS } } },
      },
      Health: {
        methods: {
          Check: { requestType: 'HealthCheckRequest', responseType: 'HealthCheckResponse' },
          Watch: { requestType: 'HealthCheckRequest', responseType: 'HealthCheckResponse', responseStream: true },
        },
      },
    } } } } } },
  },
};

/** The Health service definition (for `server.addService`). */
function healthServiceDefinition(grpc, protoLoader) {
  const definition = protoLoader.fromJSON(DESCRIPTOR, { keepCase: true, enums: String, defaults: true });
  return grpc.loadPackageDefinition(definition).grpc.health.v1.Health.service;
}

/**
 * Check/Watch handlers. `statusOf(service)` returns a SERVING_STATUS name, or
 * undefined for an unknown service (Check answers NOT_FOUND, Watch
 * SERVICE_UNKNOWN, as the health protocol says).
 */
function healthHandlers(statusOf, notFoundCode) {
  return {
    Check(call, callback) {
      const status = statusOf(call.request.service);
      if (status === undefined) callback({ code: notFoundCode, details: 'unknown service' });
      else callback(null, { status });
    },
    Watch(call) {
      // The status never changes while the process serves: send it once and
      // keep the stream open until the client cancels.
      call.write({ status: statusOf(call.request.service) ?? 'SERVICE_UNKNOWN' });
      call.on('cancelled', () => call.end());
    },
  };
}

/** A Health client constructor for `addr` (insecure, as the demo's gRPC). */
function healthClient(grpc, protoLoader, addr) {
  const Client = grpc.makeGenericClientConstructor(healthServiceDefinition(grpc, protoLoader), 'Health');
  return new Client(addr, grpc.credentials.createInsecure());
}

/** True when `service` answers SERVING within `timeoutMs`; false on any error (never rejects). */
function isServing(client, service, timeoutMs) {
  return new Promise((resolve) => {
    client.Check({ service }, { deadline: Date.now() + timeoutMs }, (err, res) => resolve(!err && res?.status === 'SERVING'));
  });
}

module.exports = { SERVING_STATUS, healthClient, healthHandlers, healthServiceDefinition, isServing };
