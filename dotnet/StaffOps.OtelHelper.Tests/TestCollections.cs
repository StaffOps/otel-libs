using System.Diagnostics;
using System.Reflection;
using OpenTelemetry;
using OpenTelemetry.Context.Propagation;
using StaffOps.OtelHelper.Tracing;
using Xunit;
using Xunit.Sdk;

namespace StaffOps.OtelHelper.Tests;

[CollectionDefinition("EnvVarTests", DisableParallelization = true)]
public class EnvVarTestsCollection { }

/// <summary>
/// After each test: the process-wide propagation an <c>AddOtelHelper</c> container installed (task C10)
/// is put back to the defaults, so the next test starts from the SDK's W3C composite, the runtime's
/// default <see cref="DistributedContextPropagator"/> and default helper settings. On every class of
/// the <c>EnvVarTests</c> collection.
/// </summary>
[AttributeUsage(AttributeTargets.Class)]
public sealed class ResetGlobalPropagationAttribute : BeforeAfterTestAttribute
{
    public override void After(MethodInfo methodUnderTest) => Reset();

    internal static void Reset()
    {
        Baggage.Current = default;
        Activity.Current = null;
        Sdk.SetDefaultTextMapPropagator(new CompositeTextMapPropagator(new TextMapPropagator[] { new TraceContextPropagator(), new BaggagePropagator() }));
        DistributedContextPropagator.Current = DistributedContextPropagator.CreateDefaultPropagator();
        HelperRuntimePropagator.GlobalSettings = PropagationSettings.Default;
    }
}
