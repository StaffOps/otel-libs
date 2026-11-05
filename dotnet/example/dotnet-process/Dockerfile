FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY Directory.Build.props Directory.Build.targets ./
COPY StaffOps.OtelHelper/StaffOps.OtelHelper.csproj StaffOps.OtelHelper/
COPY example/dotnet-process/dotnet-process.csproj example/dotnet-process/
RUN dotnet restore example/dotnet-process/dotnet-process.csproj
COPY StaffOps.OtelHelper/ StaffOps.OtelHelper/
COPY example/dotnet-process/ example/dotnet-process/
RUN dotnet publish example/dotnet-process/dotnet-process.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
ENV DOTNET_ROLL_FORWARD=Major
ENTRYPOINT ["dotnet", "dotnet-process.dll"]
