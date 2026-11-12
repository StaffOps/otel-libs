using System.Text.Json.Nodes;

namespace StaffOps.OtelHelper.ParityDriver;

/// <summary>
/// Maps a fixture `code` object (snake_case) to <see cref="TelemetryOptions"/>
/// members. An unmapped key is an adapter error (fails the case whatever its
/// status; design D-NET12). Every fixture key has a member (since task C11,
/// `http_opt_in_metrics` too).
/// </summary>
internal static class CodeMapper
{
    public static Action<TelemetryOptions> Map(JsonObject? code)
    {
        if (code is null || code.Count == 0)
        {
            return _ => { };
        }

        var unsupported = new List<string>();
        var actions = new List<Action<TelemetryOptions>>();
        foreach (var (key, value) in code.OrderBy(kv => kv.Key, StringComparer.Ordinal))
        {
            var action = MapKey(key, value!);
            if (action is null)
            {
                unsupported.Add(key);
            }
            else
            {
                actions.Add(action);
            }
        }

        if (unsupported.Count > 0)
        {
            throw new AdapterException($"unsupported code key(s) {string.Join(", ", unsupported)}");
        }

        return options =>
        {
            foreach (var action in actions)
            {
                action(options);
            }
        };
    }

    private static Action<TelemetryOptions>? MapKey(string key, JsonNode value) => key switch
    {
        "service_name" => o => o.ServiceName = value.GetValue<string>(),
        "environment" => o => o.Environment = value.GetValue<string>(),
        "otel_endpoint" => o => o.OtelCollectorEndpoint = value.GetValue<string>(),
        "traces_endpoint" => o => o.TracesEndpoint = value.GetValue<string>(),
        "metrics_endpoint" => o => o.MetricsEndpoint = value.GetValue<string>(),
        "logs_endpoint" => o => o.LogsEndpoint = value.GetValue<string>(),
        "otlp_protocol" => o => o.OtlpProtocol = value.GetValue<string>(),
        "insecure" => o => o.Insecure = value.GetValue<bool>(),
        "compression" => o => o.Compression = value.GetValue<string>(),
        "export_timeout_ms" => o => o.ExportTimeoutMs = value.GetValue<int>(),
        "export_interval_ms" => o => o.ExportIntervalMs = value.GetValue<long>(),
        "metric_exporters" => o => o.MetricExporters = string.Join(",", value.AsArray().Select(v => (string)v!)),
        "disabled_signals" => o => o.DisabledSignals = string.Join(",", value.AsArray().Select(v => (string)v!)),
        "debug" => o => o.DebugLevel = value.GetValue<bool>(),
        "sample_ratio" => o => o.SampleRatio = value.GetValue<double>(),
        "health_paths" => o => o.HealthPaths = value.AsArray().Select(v => (string)v!).ToList(),
        "prometheus_metrics_port" => o => o.PrometheusMetricsPort = value.GetValue<int>(),
        "resource_attributes" => o => MapResourceAttributes(o, value.AsObject()),
        "http_opt_in_metrics" => o => o.HttpOptInMetrics = value.GetValue<bool>(),
        _ => null,
    };

    private static void MapResourceAttributes(TelemetryOptions options, JsonObject attributes)
    {
        foreach (var (key, value) in attributes)
        {
            options.ResourceAttributes[key] = value!.GetValue<string>();
        }
    }
}
