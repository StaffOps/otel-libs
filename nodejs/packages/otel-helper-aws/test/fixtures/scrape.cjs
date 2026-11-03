'use strict';
// Calls the helper's mounted /metrics handler on an in-memory response (fixtures only).
module.exports = function scrape(helper) {
  const handler = helper.metricsHandler();
  return new Promise((resolve) => {
    const chunks = [];
    const res = {
      statusCode: 200,
      headersSent: false,
      setHeader() {},
      getHeader() {},
      removeHeader() {},
      writeHead() {
        return this;
      },
      write(chunk) {
        chunks.push(String(chunk));
        return true;
      },
      end(chunk) {
        if (chunk !== undefined && typeof chunk !== 'function') chunks.push(String(chunk));
        resolve(chunks.join(''));
        return this;
      },
    };
    handler({ method: 'GET', url: '/metrics', headers: {} }, res);
  });
};
