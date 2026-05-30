FROM mcr.microsoft.com/dotnet/sdk:10.0 AS build
WORKDIR /src
COPY OtelHelper/OtelHelper.csproj OtelHelper/
COPY example/dotnet-api/dotnet-api.csproj example/dotnet-api/
COPY example/Protos/ example/Protos/
RUN dotnet restore example/dotnet-api/dotnet-api.csproj
COPY OtelHelper/ OtelHelper/
COPY example/dotnet-api/ example/dotnet-api/
RUN dotnet publish example/dotnet-api/dotnet-api.csproj -c Release -o /app

FROM mcr.microsoft.com/dotnet/aspnet:10.0
WORKDIR /app
COPY --from=build /app .
ENV ASPNETCORE_URLS=http://+:8080
EXPOSE 8080
ENTRYPOINT ["dotnet", "dotnet-api.dll"]
