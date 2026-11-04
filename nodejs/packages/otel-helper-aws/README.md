# @staffops/otel-helper-aws

AWS SDK for JavaScript v3 instrumentation for
[`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md), through the official
`@opentelemetry/instrumentation-aws-sdk`: every `@aws-sdk/client-*` 3.x
client.

> **Not released yet** (`0.3.0-dev.0`); install it from the same release as
> the core (the peer dependency pins the same version).

```js
const { setup } = require('@staffops/otel-helper');
const { awsInstrumentation } = require('@staffops/otel-helper-aws');

setup({ instrumentations: [awsInstrumentation()] }); // no options yet: any key throws
const { S3Client } = require('@aws-sdk/client-s3'); // every send() produces a span
```

- One span per `send()`, retries included, no HTTP span under it, with the
  legacy `rpc.system=aws-api`, `rpc.service`, `rpc.method`,
  `cloud.region`, `aws.region`, `aws.request_id` and the service
  attributes (S3, DynamoDB, SQS, SNS, Lambda).
- Never recorded: credentials, request and response bodies, DynamoDB
  `item_collection_metrics`, an SNS phone number (`phone_number:**`).
- A failed call keeps status ERROR without a description; `error.type` is
  the AWS error code (`NoSuchKey`), the status name of a body-less error
  (`Forbidden`), a transport error's Node `code` (`ECONNREFUSED`), else
  `_OTHER`. The exception event keeps the AWS error message.
- A streamed S3 `Body` emits its events in the SDK's suppressed HTTP
  context: wrap traced work in its listeners in `context.with(saved, …)`.
- The AWS SDK stays your app's dependency; ES module apps need no loader
  hooks for it.

Exports (the 0.x API contract, [API stability](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md#api-stability-0x)):
`awsInstrumentation(options?)`, `VERSION`; types `AwsInstrumentationOptions`, `AwsInstrumentation`.

Details and caveats: [HOW-TO §9](https://github.com/StaffOps/otel-libs/blob/main/nodejs/HOW-TO.md#aws-sdk-v3-staffopsotel-helper-aws).
Peer dependencies: `@staffops/otel-helper` (same version),
`@opentelemetry/api` `>=1.10.0 <1.11.0`. Node.js 22.15.0 or newer.
License: Apache-2.0.
