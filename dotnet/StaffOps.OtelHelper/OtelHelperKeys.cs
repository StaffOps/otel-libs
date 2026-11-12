namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Keys of the helper's keyed DI services (requirement DN5). Inject the service's
    /// <see cref="System.Diagnostics.ActivitySource"/> and <see cref="System.Diagnostics.Metrics.Meter"/>
    /// with <c>[FromKeyedServices(OtelHelperKeys.Instrumentation)]</c> or
    /// <c>GetRequiredKeyedService&lt;ActivitySource&gt;(OtelHelperKeys.Instrumentation)</c>.
    /// They are not registered unkeyed: an unkeyed <c>ActivitySource</c> would replace the one
    /// ASP.NET Core resolves for its server spans.
    /// </summary>
    public static class OtelHelperKeys
    {
        /// <summary>Key of the <c>ActivitySource</c> and <c>Meter</c> named after the service.</summary>
        public const string Instrumentation = "StaffOps.OtelHelper.Instrumentation";
    }
}
