using System.Collections.Generic;
using System.Linq;
using OpenTelemetry.Resources;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// The resource of the three providers, from <see cref="ResolvedTelemetry"/> (the 0.2
    /// rule, kept until task C6: service name, <c>deployment.environment.name</c>, then the
    /// code attributes).
    /// </summary>
    internal static class ResourceSetup
    {
        internal static void Apply(ResourceBuilder resource, ResolvedTelemetry resolved)
        {
            resource.AddService(serviceName: resolved.ServiceName);
            // "deployment.environment.name" is the semconv >= v1.27 key
            // (not the legacy "deployment.environment") — kept in sync with Go/Python.
            resource.AddAttributes(new[]
            {
                new KeyValuePair<string, object>("deployment.environment.name", resolved.Environment),
            });
            if (resolved.ResourceAttributes.Count > 0)
            {
                resource.AddAttributes(resolved.ResourceAttributes.Select(kv => new KeyValuePair<string, object>(kv.Key, kv.Value)));
            }
        }
    }
}
