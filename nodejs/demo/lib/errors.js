// The demo's exception types (specs/demo-environment DE-D8). The names are the
// pinned literals every language uses as error.type and exception.type, so one
// query matches all four; classes set `this.name` (the Node.js exception type
// is the error's name) and carry no `code`, which the helper's log bridge
// would prefer as exception.type.
'use strict';

/** business_error: the backend's business rule rejects the order. */
class OrderRejectedError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = 'OrderRejectedError';
  }
}

/**
 * db_error: the insert hit the trigger-raised 23505. The driver error is kept
 * as the non-enumerable `driverError`, not as `cause`: a log serializer would
 * join the cause's message into exception.message, which must equal the
 * pinned message (DE-D8).
 */
class OrderExistsError extends Error {
  constructor(message, driverError) {
    super(message);
    this.name = 'OrderExistsError';
    Object.defineProperty(this, 'driverError', { value: driverError, enumerable: false });
  }
}

/** unhandled: thrown by the api in demo.order.accept and left to the framework (500). */
class DemoUnhandledError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = 'DemoUnhandledError';
  }
}

module.exports = { DemoUnhandledError, OrderExistsError, OrderRejectedError };
