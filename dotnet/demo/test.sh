#!/bin/sh
# Builds and tests the .NET demo apps (specs/demo-environment DE3d) in
# mcr.microsoft.com/dotnet/sdk:10.0, from a read-only mount of the repository:
#
#   docker run --rm -v "$PWD:/repo:ro" mcr.microsoft.com/dotnet/sdk:10.0@sha256:fe0fd4e93bd2fbb47c883b7a6369e5c8d3db8bf01cdc5981f6ee35fbdf3ff4fb /repo/dotnet/demo/test.sh
#
# The tree is copied to /work first, so the build never writes bin/ or obj/
# into the working copy (the helper libraries' obj/ stays the host's).
# Coverage (coverlet, cobertura) covers the Demo.* assemblies except the
# generated gRPC code; the gate is 90 % of lines (dev-environment rule).
set -eu
mkdir -p /work
cd /repo
for d in dotnet/Directory.Build.props dotnet/Directory.Build.targets dotnet/StaffOps.OtelHelper \
         dotnet/StaffOps.OtelHelper.Sql dotnet/StaffOps.OtelHelper.Redis dotnet/demo demo/proto demo/sql; do
  tar --exclude=bin --exclude=obj -cf - "$d" | tar -xf - -C /work
done
cd /work
# The test host needs time to let coverlet write its hit files (dotnet/TESTS.md).
export VSTEST_TESTHOST_SHUTDOWN_TIMEOUT=10000
dotnet test dotnet/demo/Tests/Demo.Tests.csproj -c Release --nologo \
  /p:CollectCoverage=true /p:CoverletOutputFormat=cobertura /p:CoverletOutput=/work/coverage/ \
  "/p:Include=[Demo.*]*" "/p:Exclude=[Demo.Tests]*%2c[*]StaffOps.OtelLibs.Demo.V1.*" \
  "/p:ExcludeByFile=**/Program.cs" /p:Threshold=90 /p:ThresholdType=line "$@"
