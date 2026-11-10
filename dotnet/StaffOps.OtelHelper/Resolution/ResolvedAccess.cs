using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Options;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Reads <see cref="ResolvedTelemetry"/> from a deferred configuration step. An invalid code
    /// value makes the resolution throw <see cref="OptionsValidationException"/>; a provider or
    /// logger built before host start (the host's first logger, an early <c>MeterProvider</c>
    /// resolve) must not fail there with another exception, so the step reports one ERROR
    /// through the sink and configures nothing; host start then fails through
    /// <c>ValidateOnStart</c> with the options exception (design D-NET6).
    /// </summary>
    internal static class ResolvedAccess
    {
        internal static bool TryGet(IServiceProvider sp, out ResolvedTelemetry resolved)
        {
            try
            {
                resolved = sp.GetRequiredService<ResolvedTelemetry>();
                return true;
            }
            catch (OptionsValidationException ex)
            {
                sp.GetService<IOtelHelperDiagnostics>()?.ReportOnce("invalid-code-options", DiagnosticLevel.Error,
                    "invalid TelemetryOptions set in code; telemetry is not configured and host start fails: " + ex.Message);
                resolved = null!;
                return false;
            }
        }
    }
}
