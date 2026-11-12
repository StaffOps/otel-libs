# Build context: dotnet/ (the helper source must be in the context):
#   docker build -t dotnet-backend -f dotnet/example/dotnet-backend/Dockerfile dotnet
# Stage "pack" packs StaffOps.OtelHelper from this tree; the example restores
# that .nupkg from the local feed /packages (example/nuget.packages.config),
# never a project reference (task C17). Dockerfile.demo takes CI's packages instead.
FROM mcr.microsoft.com/dotnet/sdk:8.0 AS pack
WORKDIR /src
# Only the core project: the examples use no opt-in package.
COPY Directory.Build.props Directory.Build.targets ./
COPY StaffOps.OtelHelper/ StaffOps.OtelHelper/
RUN dotnet pack StaffOps.OtelHelper/StaffOps.OtelHelper.csproj -c Release -o /packages --nologo

FROM mcr.microsoft.com/dotnet/sdk:8.0 AS build
WORKDIR /src
COPY --from=pack /packages/ /packages/
COPY Directory.Build.props Directory.Build.targets ./
COPY example/nuget.packages.config example/
COPY example/Protos/ example/Protos/
COPY example/dotnet-backend/dotnet-backend.csproj example/dotnet-backend/
# Exactly one core package (StaffOps.OtelHelper.<version>.nupkg): its version is
# passed as OtelHelperVersion, which switches the csproj to a PackageReference.
RUN set -- /packages/StaffOps.OtelHelper.[0-9]*.nupkg && \
    if [ $# -ne 1 ] || [ ! -f "$1" ]; then echo "expected exactly one StaffOps.OtelHelper.<version>.nupkg in /packages, found: $*" >&2; exit 1; fi && \
    ver=$(basename "$1" .nupkg | sed 's/^StaffOps\.OtelHelper\.//') && echo "$ver" > /tmp/helper-version && \
    dotnet restore example/dotnet-backend/dotnet-backend.csproj --configfile example/nuget.packages.config -p:OtelHelperVersion="$ver"
COPY example/dotnet-backend/ example/dotnet-backend/
RUN dotnet publish example/dotnet-backend/dotnet-backend.csproj -c Release --no-restore \
    -p:OtelHelperVersion="$(cat /tmp/helper-version)" -o /app

FROM mcr.microsoft.com/dotnet/aspnet:8.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_HTTP_PORTS=5100
ENV ASPNETCORE_URLS=http://+:5100
EXPOSE 5100
ENTRYPOINT ["dotnet", "dotnet-backend.dll"]
