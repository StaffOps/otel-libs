using Microsoft.AspNetCore.Routing;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper.Tracing
{
    /// <summary>
    /// Owner decision O7 (b), design D-NET8: ASP.NET Core instrumentation traces gRPC server calls
    /// with the RPC conventions only when the application sets
    /// <see cref="GrpcInstrumentationKey"/> to <c>true</c> in its configuration; the helper never sets
    /// it. When the application has mapped gRPC endpoints and the key is not true, the helper warns
    /// once, at <c>ApplicationStarted</c> (the endpoints exist then).
    /// </summary>
    internal static class GrpcServerTracingCheck
    {
        /// <summary>The experimental key the AspNetCore instrumentation reads from the application <c>IConfiguration</c>.</summary>
        internal const string GrpcInstrumentationKey = "OTEL_DOTNET_EXPERIMENTAL_ASPNETCORE_ENABLE_GRPC_INSTRUMENTATION";

        /// <summary>
        /// The endpoint metadata type gRPC for ASP.NET Core adds to every gRPC method endpoint
        /// (Grpc.AspNetCore.Server 2.62.0, checked by a test). Matched by name: the core takes no
        /// Grpc.AspNetCore dependency.
        /// </summary>
        internal const string GrpcMethodMetadataType = "Grpc.AspNetCore.Server.GrpcMethodMetadata";

        internal const string WarningText =
            "gRPC endpoints are mapped but " + GrpcInstrumentationKey + " is not true in the application configuration: " +
            "gRPC server spans are HTTP-shaped (POST /pkg.Svc/Method, status from the HTTP status 200), without the RPC conventions; set it to true to trace them as RPC spans";

        /// <summary>Warns once when gRPC endpoints are mapped, traces are exported and the key is not true.</summary>
        internal static void Run(IServiceProvider services, ResolvedTelemetry resolved, IOtelHelperDiagnostics diagnostics)
        {
            if (resolved.ResolvedSignalExporters("traces").Count == 0
                || KeyIsTrue(services.GetService<IConfiguration>())
                || !HasGrpcEndpoints(services.GetService<EndpointDataSource>()))
            {
                return;
            }

            diagnostics.ReportOnce("grpc-server-tracing-key", DiagnosticLevel.Warn, WarningText, ("variable", GrpcInstrumentationKey));
        }

        /// <summary>The key read as the instrumentation reads it: <c>bool.TryParse</c> of the configuration value.</summary>
        internal static bool KeyIsTrue(IConfiguration? configuration)
            => configuration?[GrpcInstrumentationKey] is { } value && bool.TryParse(value, out var on) && on;

        internal static bool HasGrpcEndpoints(EndpointDataSource? endpoints)
            => endpoints is not null
                && endpoints.Endpoints.Any(e => e.Metadata.Any(m => m.GetType().FullName == GrpcMethodMetadataType));
    }
}
