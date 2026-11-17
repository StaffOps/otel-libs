// The demo's exception types (specs/demo-environment DE-D8). The names are the
// pinned literals every language uses as error.type and exception.type, so one
// query matches all four. Each class returns its literal under
// `Symbol.for('otel.error_type')`, the per-error override the helper's
// withSpan / recordFailure read (contract design D15, A45); classes also set
// `this.name` and carry no `code`.
'use strict';

/** The helper's namespaced error.type override key (contract design D15). */
const ERROR_TYPE = Symbol.for('otel.error_type');

/** business_error: the backend's business rule rejects the order. */
class OrderRejectedError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = 'OrderRejectedError';
  }

  get [ERROR_TYPE]() {
    return 'OrderRejectedError';
  }
}

/**
 * db_error: the insert hit the trigger-raised 23505. The driver error is kept
 * as the non-enumerable `driverError`, not as `cause`, so the failure record's
 * exception.message stays the pinned message (DE-D8).
 */
class OrderExistsError extends Error {
  constructor(message, driverError) {
    super(message);
    this.name = 'OrderExistsError';
    Object.defineProperty(this, 'driverError', { value: driverError, enumerable: false });
  }

  get [ERROR_TYPE]() {
    return 'OrderExistsError';
  }
}

/** unhandled: thrown by the api in demo.order.accept and left to the framework (500). */
class DemoUnhandledError extends Error {
  constructor(message, options) {
    super(message, options);
    this.name = 'DemoUnhandledError';
  }

  get [ERROR_TYPE]() {
    return 'DemoUnhandledError';
  }
}

module.exports = { DemoUnhandledError, ERROR_TYPE, OrderExistsError, OrderRejectedError };
