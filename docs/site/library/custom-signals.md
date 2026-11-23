# Custom spans, metrics and logs

How to add your own telemetry on top of the helper's automatic
instrumentation, the same way in every language: a span that follows the
failure rule, a counter and a histogram, and a log correlated with the span.

**Audience:** application engineers.
**Prerequisites:** the helper set up (`Setup`, `setup_telemetry`, `setup`,
`AddOtelHelper`), see the [Overview](../index.md#quick-start).

## The rules

### Spans

- Get the tracer from the helper (`GetTracer`, `get_tracer`, `getTracer`) or,
  in .NET, an `ActivitySource` registered with the helper. The default scope
  name is shared across languages.
- Start a **root span** for work that is not caused by a request (a batch, a
  scheduled job): `StartRootSpan` / `start_root_span` / `startRootSpan` /
  `StartRootActivity`. Inside a request, start a child span.
- Name spans by operation, not by data: `order.process`, never
  `order 4711`. Put identifiers in attributes, and only bounded ones in
  metrics.
- **Failure status** ([ADR-016](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-016-application-span-failure-status.md)):
  a failed span is ERROR **without a description** and with `error.type`; a
  successful span stays **Unset** (never Ok); a cancellation is not a failure.
  Go `WithSpan` / `RecordFailure`, Python `traced` / `record_failure` and
  Node.js `withSpan` / `recordFailure` apply the rule for you. In .NET the
  failure helper is parked (contract task A45 .NET): set the status and
  `error.type` yourself ([.NET HOW-TO §5](../dotnet/HOW-TO.md#5-traces)). The
  example apps predate this rule; see the warning under
  [The example apps](#the-example-apps).

### Exception detail goes to a log

The span keeps only the exception **type**. The message and the stack trace
go to **one ERROR log record written inside the span**, so it carries the
span's `trace_id` and `span_id`
([ADR-014](https://github.com/StaffOps/otel-libs/blob/main/specs/decisions/ADR-014-exception-detail-in-correlated-logs.md)).
The failure helpers write that record for you, with `exception.type`,
`exception.message` and `exception.stacktrace`. The record is written for an
unsampled span too: see [the unsampled dead link](../observe/correlation.md#unsampled-records-and-the-dead-trace-link).

### Metrics

- Get the meter from the helper (`GetMeter`, `get_meter`, `getMeter`, the
  keyed .NET `Meter`).
- Pick the instrument by what you measure: a **counter** for things that
  happen (`order.processed`), a **histogram** for durations and sizes
  (`order.duration`, unit `s`), an **up-down counter** for things in flight,
  an **observable gauge** for a value you sample (queue depth).
- Set the unit (`s`, `By`, `{order}`); the Prometheus name gets the suffix
  (`order_duration_seconds`, `order_processed_total`).
- Attributes must be bounded (status, channel, `error.type`), never user
  ids, order ids or messages: each value combination is a time series.
- Histograms recorded inside a sampled span carry an **exemplar**, a link
  from the metric point to the trace (Node.js: none).

### Logs

- Log through the language's normal API, inside the span: Go `slog` with the
  helper's handler (`logger.ErrorContext(ctx, …)`), Python `logging`, Node.js
  `createLogger()` (or the pino / winston packages), .NET `ILogger`.
- A record written in a span carries `trace_id` and `span_id`; Loki stores
  them as structured metadata, which is what the Grafana links use
  ([Logs](../observe/signals/logs.md)).
- Put the detail in attributes (`order.id`), keep the message stable
  (`"order failed"`), so you can count and filter records.

## The patterns, per language

Each snippet processes one order inside a span that follows the failure rule
(a raised or returned error marks the span ERROR with `error.type`, and the
helper writes the correlated failure record), counts it, records its
duration, and writes one log of its own.

=== "Go"

    ```go
    tracer := otelhelper.GetTracer()
    meter := otelhelper.GetMeter()
    processed, _ := meter.Int64Counter("order.processed", metric.WithUnit("{order}"))
    duration, _ := meter.Float64Histogram("order.duration", metric.WithUnit("s"))
    logger := slog.New(otelhelper.NewSlogHandler())

    err := otelhelper.WithSpan(ctx, tracer, "order.process", func(ctx context.Context) error {
        start := time.Now()
        err := charge(ctx, order) // a returned error marks the span; the helper logs the detail
        status := "ok"
        if err != nil {
            status = "failed"
        }
        attrs := metric.WithAttributes(attribute.String("order.status", status))
        processed.Add(ctx, 1, attrs)
        duration.Record(ctx, time.Since(start).Seconds(), attrs)
        logger.InfoContext(ctx, "order handled", "order.id", order.ID, "order.status", status)
        return err
    })

    // Inside a span, for a failure the code handles instead of returning:
    // otelhelper.RecordFailure(ctx, &OrderRejectedError{ID: order.ID})
    ```

    A root span for a batch: `otelhelper.StartRootSpan(ctx, tracer, "orders.process-batch")`,
    or `trace.WithNewRoot()` in the `WithSpan` options.

=== "Python"

    ```python
    import logging
    import time

    from staffops_otel_helper import get_meter, record_failure, traced

    meter = get_meter()
    processed = meter.create_counter("order.processed", unit="{order}")
    duration = meter.create_histogram("order.duration", unit="s")
    log = logging.getLogger("orders")

    def process(order):
        with traced("order.process"):   # an escaping exception marks the span
            start = time.monotonic()
            status = "ok"
            try:
                charge(order)
            except PaymentDeclined as exc:      # handled: mark it, do not re-raise
                record_failure(exc)
                status = "failed"
            attrs = {"order.status": status}
            processed.add(1, attrs)
            duration.record(time.monotonic() - start, attrs)
            log.info("order handled", extra={"order.id": order.id, "order.status": status})
    ```

    A root span for a batch: `traced("orders.process-batch", root=True)`.
    `traced` also works as a decorator of a plain or `async def` function.

=== "Node.js"

    ```javascript
    const { createLogger, getMeter, recordFailure, withSpan } = require('@staffops/otel-helper');

    const meter = getMeter();
    const processed = meter.createCounter('order.processed', { unit: '{order}' });
    const duration = meter.createHistogram('order.duration', { unit: 's' });
    const logger = createLogger({ name: 'orders' });

    async function process(order) {
      return withSpan('order.process', async () => {   // a throw or rejection marks the span
        const start = performance.now();
        let status = 'ok';
        try {
          await charge(order);
        } catch (err) {
          if (!(err instanceof PaymentDeclined)) throw err;
          recordFailure(err);                        // handled: mark the active span
          status = 'failed';
        }
        const attrs = { 'order.status': status };
        processed.add(1, attrs);
        duration.record((performance.now() - start) / 1000, attrs);
        logger.info('order handled', { 'order.id': order.id, 'order.status': status });
      });
    }
    ```

    A root span for a batch: `startRootSpan('orders.process-batch', fn)`.
    The package names are in [Consuming packages](../CONSUMING.md#nodejs-npm-packages).

=== ".NET"

    ```csharp
    // ActivitySource "orders" registered with opts.AdditionalActivitySources.Add("orders")
    private static readonly ActivitySource Source = new("orders");
    private static readonly Counter<long> Processed =
        Meter.CreateCounter<long>("order.processed", "{order}");
    private static readonly Histogram<double> Duration =
        Meter.CreateHistogram<double>("order.duration", "s");

    void Process(Order order)
    {
        using var activity = Source.StartActivity("order.process");
        var start = Stopwatch.GetTimestamp();
        var status = "ok";
        try
        {
            Charge(order);
        }
        catch (PaymentDeclinedException ex)
        {
            // The type stays on the span; the message and stack go to a log
            // written inside the activity (ADR-014). No description, no AddException.
            activity?.SetStatus(ActivityStatusCode.Error);
            activity?.SetTag("error.type", ex.GetType().FullName);
            _logger.LogError(ex, "order failed");
            status = "failed";
        }
        var tags = new TagList { { "order.status", status } };
        Processed.Add(1, tags);
        Duration.Record(Stopwatch.GetElapsedTime(start).TotalSeconds, tags);
        _logger.LogInformation("order handled {OrderId} {OrderStatus}", order.Id, status);
    }
    ```

    .NET has no failure helper yet (contract task A45 .NET, parked): the
    `catch` block does by hand what the other helpers do. A root span:
    `Source.StartRootActivity("orders.process-batch")`.

## The example apps

Each language has a runnable `custom-signals` app with the full set of
instruments (counter, histogram, up-down counter, observable gauge):

- Go: [`go/example/go-custom-signals`](../go/example/README.md#go-custom-signals)
- Python: [`python/example/python-custom-signals`](../python/example/README.md#custom-signals)
- Node.js: [`nodejs/example/node-custom-signals`](../nodejs/example/README.md#custom-signals)
- .NET: [`dotnet/example/dotnet-custom-signals`](../dotnet/example/README.md#custom-signals-dotnet-custom-signals)

!!! warning "The example apps predate the failure rule"
    They still record the exception on the span and set a status
    description, which the rules above (ADR-014, ADR-016) replace. Use them
    for the metric and log APIs. Follow the snippets above for failures. The
    apps will be rewritten after the soak (A50 phase 2).

Each HOW-TO has the full recipe: Go
[§4](../go/HOW-TO.md#4-custom-signals) and
[§3b](../go/HOW-TO.md#3b-application-span-failures-withspan-and-recordfailure),
Python [§6](../python/HOW-TO.md#6-custom-signals-spans-metrics-logs),
Node.js [§6](../nodejs/HOW-TO.md#6-custom-signals-spans-metrics-logs) and
[§16](../nodejs/HOW-TO.md#16-application-span-failures-withspan-recordfailure),
.NET [§5 Traces](../dotnet/HOW-TO.md#5-traces) and
[§6 Metrics](../dotnet/HOW-TO.md#6-metrics).

!!! note "Screenshot (phase 2)"
    Grafana Explore, Prometheus: `sum by (service_name) (rate(demo_orders_processed_total[5m]))`
    and the heatmap of `sum by (le) (rate(demo_order_duration_seconds_bucket[$__rate_interval]))`
    with exemplars on — the custom metrics of the demo backends.

## Seeing the result

- The spans: Tempo, TraceQL `{ resource.service.name = "<your-service>" && name = "order.process" }`
  ([Traces](../observe/signals/traces.md)).
- The failure records the helper writes: Loki,
  `{service_name="<your-service>"} | severity_number >= 17 | exception_type != ""`
  ([Logs](../observe/signals/logs.md#the-failure-record)).
- Your own logs (`order handled` above are the application's records, not
  helper failure records): `{service_name="<your-service>"} | order_status != ""`.
- The metrics: `order_processed_total`, `order_duration_seconds_bucket`
  ([Metrics](../observe/signals/metrics.md)).
