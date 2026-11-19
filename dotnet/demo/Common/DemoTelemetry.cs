using System.Diagnostics;
using System.Diagnostics.Metrics;

namespace StaffOps.OtelLibs.Demo;

/// <summary>
/// The demo's own instrumentation scope (design DE-D8): every span the demo code creates comes
/// from <see cref="Source"/>, every custom instrument from <see cref="Meter"/>, both named
/// <c>otel-libs-demo</c> (check d selects demo spans by this scope). Register the name with
/// <c>AdditionalActivitySources</c> / <c>AdditionalMeters</c> so the helper exports them.
/// </summary>
public static class DemoTelemetry
{
    /// <summary>The instrumentation scope name of demo spans and instruments.</summary>
    public const string ScopeName = "otel-libs-demo";

    /// <summary>The source of every demo span.</summary>
    public static readonly ActivitySource Source = new(ScopeName);

    /// <summary>The meter of the demo's custom instruments (DE-D9).</summary>
    public static readonly Meter Meter = new(ScopeName);
}
