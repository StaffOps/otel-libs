# Runs the .NET smoke program once per combination (inside the dotnet-smoke service).
# Packs StaffOps.OtelHelper from this tree into a local feed and builds the
# smoke program against the package (no project reference), as a consumer does.
set -eu
# shellcheck disable=SC3040 # dash and bash both support pipefail
(set -o pipefail) 2>/dev/null && set -o pipefail
. ./combos.sh
# Copy out of the read-only mount without the host's bin/obj (another platform's files).
mkdir -p /work/dotnet /packages
tar -C /src/dotnet --exclude=bin --exclude=obj -cf - . | tar -C /work/dotnet -xf -
dotnet pack /work/dotnet/StaffOps.OtelHelper/StaffOps.OtelHelper.csproj -c Release -o /packages --nologo -v quiet
# Exactly one core package: its version is the smoke program's OtelHelperVersion.
set -- /packages/StaffOps.OtelHelper.[0-9]*.nupkg
if [ $# -ne 1 ] || [ ! -f "$1" ]; then
  echo "run-dotnet: expected exactly one StaffOps.OtelHelper.<version>.nupkg in /packages, found: $*" >&2
  exit 1
fi
ver=$(basename "$1" .nupkg | sed 's/^StaffOps\.OtelHelper\.//')
echo "run-dotnet: packed StaffOps.OtelHelper $ver"
cp -r /src/testing/e2e/dotnet-smoke /work/smoke
# The StaffOps.OtelHelper* packages only from the local feed /packages, the rest
# from nuget.org (dotnet/example/nuget.packages.config). The container's NuGet
# cache is its own (no cache volume): a cached package of the same version packed
# from an older tree would otherwise be restored instead of this one.
dotnet restore /work/smoke/dotnet-smoke.csproj --configfile /work/dotnet/example/nuget.packages.config \
  -p:OtelHelperVersion="$ver" -v quiet
dotnet publish /work/smoke/dotnet-smoke.csproj -c Release -o /work/out --nologo -v quiet --no-restore \
  -p:OtelHelperVersion="$ver"
echo "$COMBOS" | while IFS='|' read -r name envs; do
  # Bounded run (60 s, as run-go.sh and run-node.sh): a hung export must not hang the job.
  # shellcheck disable=SC2086 # $envs is a list of VAR=value words
  env OTEL_SERVICE_NAME="e2e-dotnet-$name" ENVIRONMENT=DEV OTEL_METRIC_EXPORT_INTERVAL=1000 \
    OTEL_HELPER_METRICS_PORT=0 $envs \
    timeout 60 dotnet /work/out/dotnet-smoke.dll || echo "dotnet-smoke[$name] exited $? (see smoke.sh expectations)"
done
