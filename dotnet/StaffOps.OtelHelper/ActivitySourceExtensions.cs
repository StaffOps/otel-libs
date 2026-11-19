using System.Diagnostics;
using System.Reflection;
using StaffOps.OtelHelper.Diagnostics;

namespace StaffOps.OtelHelper
{
    /// <summary>
    /// Extensions for ActivitySource to simplify common tracing patterns.
    /// </summary>
    public static class ActivitySourceExtensions
    {
        // Activity.Stop sets Activity.Current back to the activity that was current when Start ran.
        // StartRootActivity has to clear Activity.Current before Start (a parent is otherwise taken
        // from it), so it records the real previous activity there afterwards (design N3).
        private static readonly FieldInfo? PreviousActiveActivity =
            typeof(Activity).GetField("_previousActiveActivity", BindingFlags.Instance | BindingFlags.NonPublic);

        /// <summary>
        /// Starts a new root Activity (new trace), ignoring the current one. Use in workers and
        /// background services where each iteration is an independent trace. While the root runs it
        /// is <see cref="Activity.Current"/>; when it is stopped or disposed the previous
        /// <see cref="Activity.Current"/> is current again. When no activity is created (no listener,
        /// or the sampler drops it) <see cref="Activity.Current"/> is left as it was and null is returned.
        /// On a runtime without the field <c>Activity.Stop</c> restores, one warning goes to the
        /// helper's fallback diagnostics sink (standard error) and the previous activity is not restored.
        /// </summary>
        public static Activity? StartRootActivity(this ActivitySource source, string name)
        {
            var previous = Activity.Current;
            Activity.Current = null;
            var activity = source.StartActivity(name);
            if (activity is null)
            {
                Activity.Current = previous;
                return null;
            }

            if (previous is not null && !TrySetPrevious(activity, previous, PreviousActiveActivity))
            {
                OtelHelperDiagnostics.Fallback.ReportOnce("start-root-restore", DiagnosticLevel.Warn,
                    "StartRootActivity cannot restore the previous Activity.Current on this runtime; it is null after the root stops");
            }

            return activity;
        }

        /// <summary>Records <paramref name="previous"/> as the activity <paramref name="root"/> restores on stop; false when the runtime has no such field.</summary>
        internal static bool TrySetPrevious(Activity root, Activity previous, FieldInfo? field)
        {
            if (field?.FieldType != typeof(Activity))
            {
                return false;
            }

            field.SetValue(root, previous);
            return true;
        }
    }
}
