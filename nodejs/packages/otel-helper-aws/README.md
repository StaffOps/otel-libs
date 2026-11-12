# @staffops/otel-helper-aws

AWS SDK for JavaScript v3 instrumentation for
[`@staffops/otel-helper`](https://github.com/StaffOps/otel-libs/blob/main/nodejs/README.md), through the official
`@opentelemetry/instrumentation-aws-sdk`: every `@aws-sdk/client-*` 3.x
client.

> **Release candidate `0.3.0-rc.1`**, published to npmjs (dist-tag `next`, with
> provenance) and to GitHub Packages. Install an exact version, the same as
> the core's (the peer dependency pins it):
> `npm install @staffops/otel-helper@0.3.0-rc.1 @staffops/otel-helper-aws@0.3.0-rc.1`.
> Never a bare `npm install @staffops/otel-helper-aws`: until the first
> stable release, `latest` is a deprecated `0.0.0-bootstrap.0` placeholder.

```js
const { setup } = require('@staffops/otel-helper');
const { awsInstrumentation } = require('@staffops/otel-helper-aws');

setup({ instrumentations: [awsInstrumentation()] }); // no options yet: any key throws
const { S3Client } = require('@aws-sdk/client-s3'); // every send() produces a span
```

- One span per `send()`, retries included, no HTTP span under it, named
  `<Service>.<Operation>` (`SQS.SendMessage`; Bedrock GenAI spans keep their
  name), PRODUCER on SQS/SNS send and publish, CONSUMER on SQS receive, CLIENT
  otherwise, with the legacy `rpc.system=aws-api`, `rpc.service`, `rpc.method`
  (`OTEL_SEMCONV_STABILITY_OPT_IN=http,rpc`: `rpc.system.name` and
  `rpc.method=<Service>/<Operation>`; `http,rpc/dup`: both),
  `messaging.operation.name` = the AWS operation on SQS and SNS spans,
  `cloud.region`, `aws.region`, `aws.request_id` and the service
  attributes (S3, DynamoDB, SQS, SNS, Lambda). DynamoDB spans carry
  `db.system.name=aws.dynamodb` (semconv v1.43; the contrib writes
  `dynamodb`) and the legacy `db.system=dynamodb` unless
  `OTEL_SEMCONV_STABILITY_OPT_IN` holds `database` without `database/dup`.
  No `server.address`/`server.port`: the contrib does not see the endpoint.
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
