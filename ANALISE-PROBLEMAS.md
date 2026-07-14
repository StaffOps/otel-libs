# Análise Crítica — Problemas Identificados e Correções

> Análise do estado do repositório em `0.1.0-rc.1` (2026-07-04), comparando com o
> padrão de mercado (spec OpenTelemetry, distros comerciais como Splunk/Grafana/
> Lightstep, e convenções de empacotamento de cada ecossistema).
>
> Cada problema tem: severidade, evidência (arquivo:linha), impacto e correção
> proposta. A ordem dos itens dentro de cada seção é a ordem de prioridade
> sugerida. Um resumo priorizado está no final.

---

## P1 — Python: core não é "lightweight" e as extras `[aws]`, `[redis]`, `[sql]` são no-ops

**Severidade: Alta (bug de empacotamento publicado — contradiz a documentação)**

### Evidência

`python/pyproject.toml:16-24` instala **todas** as instrumentações como
dependência obrigatória:

```toml
dependencies = [
    ...
    # Instrumentations (always installed, activation controlled via OTEL_HELPER_EXTRA_INSTRUMENTATION)
    "opentelemetry-instrumentation-sqlalchemy==0.63b1",
    "opentelemetry-instrumentation-redis==0.63b1",
    "opentelemetry-instrumentation-botocore==0.63b1",
]

[project.optional-dependencies]
aws = ["opentelemetry-instrumentation-botocore>=0.48b0"]
redis = ["opentelemetry-instrumentation-redis>=0.48b0"]
sql = ["opentelemetry-instrumentation-sqlalchemy>=0.48b0"]
```

Como o core já traz `botocore`/`redis`/`sqlalchemy` instrumentation,
`pip install otel-helper[aws]` instala exatamente o mesmo que
`pip install otel-helper`. As extras não fazem nada.

### Impacto

- Contradiz diretamente o `README.md` ("Core packages remain lightweight — add
  only what you need") e o `CHANGELOG.md` do rc.1.
- Todo consumidor Python carrega dependências transitivas de AWS/Redis/SQL
  mesmo sem usar (paridade quebrada com .NET e Go, onde os subpacotes são
  realmente separados).

### Correção

Mover as três instrumentações condicionais para as extras (e **remover** do
core). O código já está preparado: `otel_helper/instrumentation.py:46-67` só
ativa SQL/Redis/AWS se habilitado via `OTEL_HELPER_EXTRA_INSTRUMENTATION` **e**
já engole `ImportError` — ou seja, remover a dependência do core não quebra
nada.

```toml
dependencies = [
    "opentelemetry-api>=1.42,<2",
    "opentelemetry-sdk>=1.42,<2",
    "opentelemetry-exporter-otlp-proto-grpc>=1.42,<2",
    "opentelemetry-exporter-prometheus>=0.63b0",
    "prometheus-client>=0.20.0",
    # Instrumentações "sempre ativas" (frameworks web/HTTP básicos)
    "opentelemetry-instrumentation-fastapi>=0.63b0",
    "opentelemetry-instrumentation-httpx>=0.63b0",
    "opentelemetry-instrumentation-requests>=0.63b0",
    "opentelemetry-instrumentation-grpc>=0.63b0",
    "opentelemetry-instrumentation-system-metrics>=0.63b0",
]

[project.optional-dependencies]
aws = ["opentelemetry-instrumentation-botocore>=0.63b0"]
redis = ["opentelemetry-instrumentation-redis>=0.63b0"]
sql = ["opentelemetry-instrumentation-sqlalchemy>=0.63b0"]
all = ["otel-helper[aws,redis,sql]"]
```

Complemento: nos módulos `otel_helper/ext/{aws,redis,sql}.py`, trocar o import
"seco" por uma mensagem de erro acionável quando a extra não foi instalada
(padrão das distros de mercado):

```python
def instrument_aws() -> None:
    try:
        from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    except ImportError as e:
        raise ImportError(
            "AWS instrumentation not installed. Run: pip install otel-helper[aws]"
        ) from e
    BotocoreInstrumentor().instrument()
```

---

## P2 — Python: versões pinadas com `==` em uma biblioteca

**Severidade: Alta (bloqueia adoção — conflito de resolução garantido)**

### Evidência

`python/pyproject.toml:11-24` — todas as dependências OTel usam `==1.42.1` /
`==0.63b1`.

### Impacto

Pin exato é prática de **aplicação**, não de biblioteca. Qualquer app que
dependa de outra versão do SDK OTel (ou de outra lib que dependa) entra em
conflito de resolução do pip e não consegue instalar `otel-helper`. É a
reclamação nº 1 em bibliotecas wrapper de telemetria no PyPI.

### Correção

Usar ranges compatíveis (já incluído no snippet do P1):

- `opentelemetry-*` estáveis: `>=1.42,<2`
- `opentelemetry-instrumentation-*` (0.x betas): `>=0.63b0` (as instrumentações
  seguem versionamento acoplado ao SDK; se quiser mais conservadorismo,
  `>=0.63b0,<1`).

O pin exato pode (e deve) continuar existindo — mas no lockfile/imagem dos
**exemplos e demos** (`python/example/*/requirements.txt`), não no
`pyproject.toml` da lib.

---

## P3 — Python: porta customizada do endpoint OTLP é silenciosamente descartada

**Severidade: Alta (perda silenciosa de telemetria — mesma família do bug corrigido no rc.1)**

### Evidência

`python/otel_helper/config.py:62` reconstrói o endpoint só com scheme+host:

```python
return f"{parsed.scheme}://{parsed.hostname}"   # porta descartada aqui
```

e `config.py:136` sempre anexa a porta padrão:

```python
self.otel_endpoint = f"{collector_host}:{_DEFAULT_OTLP_PORT}"   # sempre :4317
```

Se o operador setar `OTEL_EXPORTER_OTLP_ENDPOINT=https://gateway:14317`, a lib
exporta para `https://gateway:4317` sem nenhum aviso.

**Go e .NET não têm esse bug** — verificado: `go/config.go:68-73` preserva a
porta (`u.Port()`, default 4317 só quando ausente) e
`dotnet/OtelHelper/Models/TelemetryOptionsPostConfigure.cs:111` faz o mesmo
(`uri.IsDefaultPort ? 4317 : uri.Port`). O Python está fora de paridade.

### Correção

Alinhar `_resolve_collector_host()` com a lógica do Go/.NET — preservar a porta
quando informada, aplicar 4317 apenas como default:

```python
def _resolve_collector_host() -> str:
    env = os.getenv(ENV_COLLECTOR_ENDPOINT, "").strip().rstrip("/")
    if not env:
        return ""
    if "://" not in env:
        insecure = os.getenv(ENV_INSECURE, "").strip().lower() == "true"
        env = f"{'http' if insecure else 'https'}://{env}"
    parsed = urlparse(env)
    if not parsed.hostname:
        return env
    port = parsed.port or _DEFAULT_OTLP_PORT
    return f"{parsed.scheme}://{parsed.hostname}:{port}"
```

E em `resolve_from_env()`, parar de anexar `:4317` (a porta já vem resolvida):

```python
if not self.otel_endpoint and collector_host:
    self.otel_endpoint = collector_host
```

Adicionar teste em `tests/test_config.py`: endpoint com porta custom
(`https://gw:14317` → preserva 14317), sem porta (`https://gw` → 4317),
schemeless com porta (`gw:14317` → preserva).

---

## P4 — Feature solicitada: `/metrics` **em conjunto** com OTLP (hoje é ou-um-ou-outro)

**Severidade: Feature (é o pedido do usuário) — não requer subpacote novo**

### Estado atual

O fallback Prometheus já existe nas três linguagens, mas só quando
`OTEL_EXPORTER_OTLP_ENDPOINT` **não** está setado:

- `python/otel_helper/metrics.py:31-51` — `if options.otel_endpoint: OTLP else: Prometheus`
- `go/metrics.go:32-63` — mesma estrutura
- `dotnet/OtelHelper/MetricsSetup.cs:23-38` — mesma estrutura

Não há como ter OTLP push **e** `/metrics` simultaneamente (ex.: ambiente com
Collector para traces/logs mas scrape Prometheus legado para métricas; ou
migração gradual).

### Por que NÃO criar subpacote

1. As dependências Prometheus já estão no **core** das três linguagens
   (`prometheus-client` no pyproject, `otel/exporters/prometheus` no go.mod,
   `OpenTelemetry.Exporter.Prometheus.HttpListener` no csproj). Separar agora
   não reduz tamanho e seria breaking change no meio do rc.
2. O spec OTel suporta **múltiplos `MetricReader`s no mesmo `MeterProvider`** —
   o mesmo instrumento é exportado por push OTLP e exposto para scrape ao
   mesmo tempo. É mudança de configuração (~10 linhas por linguagem), não um
   pacote.
3. Padrão de mercado: as distros honram a env var **padrão do SDK**
   `OTEL_METRICS_EXPORTER` (lista separada por vírgula: `otlp`, `prometheus`,
   `none`) em vez de inventar flag proprietária.

### Correção (procedimento)

**Contrato proposto** (retrocompatível):

| `OTEL_METRICS_EXPORTER` | Comportamento |
|---|---|
| _(não setado)_ | Atual: OTLP se tem endpoint, senão fallback Prometheus |
| `otlp` | Só OTLP (falha na validação se não houver endpoint) |
| `prometheus` | Só `/metrics`, mesmo com endpoint OTLP setado |
| `otlp,prometheus` | **Os dois readers no mesmo provider** |
| `none` | Métricas desabilitadas (equivalente a `OTEL_HELPER_DISABLED_SIGNALS=metrics`) |

Com opção programática equivalente: `metric_exporters=["otlp","prometheus"]`
(Python), `WithMetricExporters("otlp", "prometheus")` (Go),
`options.MetricExporters` (.NET).

**Implementação por linguagem** — trocar o `if/else` por acúmulo de readers:

Python (`metrics.py`):

```python
readers = []
if "otlp" in exporters and options.otel_endpoint:
    readers.append(PeriodicExportingMetricReader(otlp_exporter, export_interval_millis=30_000))
if "prometheus" in exporters:
    start_http_server(port=options.prometheus_metrics_port)
    readers.append(PrometheusMetricReader())
provider = MeterProvider(resource=resource, metric_readers=readers, ...)
```

Go (`metrics.go`): ambos os branches fazem `append` em `mpOpts` com
`sdkmetric.WithReader(...)` — remover a exclusividade do `if/else` e iniciar o
servidor HTTP sempre que o reader Prometheus estiver na lista.

.NET (`MetricsSetup.cs`): `AddOtlpExporter(...)` e
`AddPrometheusHttpListener(...)` no mesmo builder — o SDK .NET já suporta.

**Sobre expor "apenas algumas métricas" no `/metrics`:** o SDK OTel não suporta
Views por-reader — um filtro (`OTEL_HELPER_DISABLED_METRICS`) se aplica ao
provider inteiro, afetando OTLP e Prometheus igualmente. Para expor um
subconjunto no scrape mantendo tudo no OTLP, o padrão de mercado é **filtrar no
lado do scraper** (`metric_relabel_configs` do Prometheus / `keep`/`drop` no
vmagent). Recomendação: documentar isso no HOW-TO com um exemplo de relabel, em
vez de manter um segundo `MeterProvider` paralelo (complexidade alta, métricas
duplicadas, footprint dobrado).

---

## P5 — Go: servidor `/metrics` frágil (erro engolido, sem timeouts, sem shutdown)

**Severidade: Média-Alta (falha silenciosa — a classe de bug que o rc.1 se propôs a eliminar)**

### Evidência

`go/metrics.go:78-85`:

```go
go func() {
    mux := http.NewServeMux()
    mux.Handle("/metrics", promhttp.Handler())
    srv := &http.Server{Addr: fmt.Sprintf(":%d", opts.PrometheusMetricsPort), Handler: mux}
    srv.ListenAndServe()   // erro ignorado
}()
```

Quatro problemas:

1. **Erro de `ListenAndServe` ignorado.** Porta 9464 ocupada → nenhuma métrica
   exposta e nenhum log. Exatamente "silent telemetry loss".
2. **Sem `ReadHeaderTimeout`** → vulnerável a Slowloris (gosec G112).
3. **Servidor fora da cadeia de shutdown** — `Setup` retorna `Shutdown` que
   fecha os providers (`otelhelper.go:90-96`), mas o `http.Server` vaza.
4. **`promhttp.Handler()` usa o registry global do client_golang**, não um
   registry ligado ao exporter OTel. Se a aplicação também usa client_golang,
   há acoplamento acidental (métricas do app aparecem no /metrics da lib e
   vice-versa).

### Correção

```go
registry := prometheus.NewRegistry()
exporter, err := promexporter.New(promexporter.WithRegisterer(registry))
if err != nil { ... }

mux := http.NewServeMux()
mux.Handle("/metrics", promhttp.HandlerFor(registry, promhttp.HandlerOpts{}))
srv := &http.Server{
    Addr:              fmt.Sprintf(":%d", opts.PrometheusMetricsPort),
    Handler:           mux,
    ReadHeaderTimeout: 5 * time.Second,
}
go func() {
    if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
        otel.Handle(fmt.Errorf("otelhelper: prometheus /metrics server: %w", err))
    }
}()
```

E registrar `srv.Shutdown` na cadeia de shutdowns retornada por `Setup` (em
`otelhelper.go`, junto com `mp.Shutdown`). Para diagnóstico imediato de porta
ocupada, considerar `net.Listen` síncrono antes da goroutine — aí `Setup`
retorna erro na hora em vez de falhar em background.

---

## P6 — Python: `start_http_server` quebra com múltiplos workers (gunicorn/uvicorn)

**Severidade: Média-Alta (o caso de uso mais comum de FastAPI em produção)**

### Evidência

`python/otel_helper/metrics.py:46` — `start_http_server(port=...)` abre um
socket por processo. Com `gunicorn -w 4`, o 2º worker morre (ou o setup falha)
com `Address already in use`; e mesmo que funcionasse, cada worker exporia só
as próprias métricas na mesma porta.

### Correção

Duas frentes (padrão de mercado):

1. **Oferecer handler montável no app** em vez de servidor separado — para web
   apps, expor `/metrics` no próprio servidor da aplicação resolve o problema
   de porta e de descoberta:

```python
# otel_helper/metrics.py
def metrics_app():
    """ASGI app para montar no FastAPI/Starlette: app.mount('/metrics', metrics_app())"""
    from prometheus_client import make_asgi_app
    return make_asgi_app()
```

2. **Documentar a limitação** no HOW-TO: o listener standalone (porta 9464) é
   para processos worker/CLI sem servidor HTTP e single-process; para gunicorn
   multi-worker, usar o mount (cada worker responde pelo scrape do app) ou o
   modo multiprocess do `prometheus_client` (`PROMETHEUS_MULTIPROC_DIR`).

O equivalente existe nas outras linguagens e vale expor também:
- Go: exportar `otelhelper.MetricsHandler() http.Handler` para o app montar no
  próprio mux.
- .NET: para apps ASP.NET Core, o padrão é
  `OpenTelemetry.Exporter.Prometheus.AspNetCore` +
  `app.MapPrometheusScrapingEndpoint()` (o `HttpListener` é para processos sem
  pipeline ASP.NET). Vale oferecer os dois caminhos.

---

## P7 — Env vars proprietárias onde existe padrão OTel (e 30s hardcoded)

**Severidade: Média (fricção de adoção; corrigir ANTES do 0.1.0 estável para não virar breaking change)**

### Evidência

| Custom atual | Padrão OTel equivalente | Onde |
|---|---|---|
| `OTEL_HELPER_SAMPLE_RATIO` | `OTEL_TRACES_SAMPLER=parentbased_traceidratio` + `OTEL_TRACES_SAMPLER_ARG` | `config.py:24`, `go/config.go:30` |
| intervalo 30s hardcoded | `OTEL_METRIC_EXPORT_INTERVAL` (ms) | `metrics.py:40`, `go/metrics.go:60`, `MetricsSetup.cs:29` |
| _(inexistente)_ | `OTEL_METRICS_EXPORTER` | ver P4 |
| `ENVIRONMENT` | `OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=...` | `config.py:21` |

Um operador que já conhece OTel seta a var padrão e não entende por que não
funcionou. Distros de mercado (Splunk, Grafana, Lightstep) sempre honram o spec
primeiro; as vars proprietárias são açúcar por cima.

### Correção

Regra de precedência: **código explícito > env var OTel padrão > env var
`OTEL_HELPER_*` > default da lib.** Concretamente:

1. **Intervalo de métricas**: trocar o `30_000` hardcoded por
   `int(os.getenv("OTEL_METRIC_EXPORT_INTERVAL", "30000"))` (e equivalentes em
   Go/.NET — no .NET o SDK já lê essa var sozinho se não for sobrescrita no
   código; hoje o hardcode em `MetricsSetup.cs:29` a atropela).
2. **Sampler**: se `OTEL_TRACES_SAMPLER` estiver setado, respeitar e não
   aplicar `OTEL_HELPER_SAMPLE_RATIO`.
3. **`OTEL_METRICS_EXPORTER`**: implementar via P4.
4. Manter as vars `OTEL_HELPER_*` funcionando (são convenientes), apenas com
   precedência menor que as do spec. Documentar a tabela de precedência no
   README.

---

## P8 — Semconv divergente: `deployment.environment` vs `deployment.environment.name`

**Severidade: Média (quebra os dashboards compartilhados entre linguagens)**

### Evidência

`go/otelhelper.go:123` emite a chave literal antiga:

```go
attribute.String("deployment.environment", string(opts.Environment)),
```

com semconv `v1.26.0` (`go/otelhelper.go:15`). Desde o semconv v1.27, o
atributo padrão é **`deployment.environment.name`**. Se .NET/Python emitirem
uma chave e o Go outra (ou se o Collector fizer a promoção com a chave nova),
os dashboards em `dashboards/*.json` — que são "compatible with any language" —
deixam de bater entre linguagens.

### Correção

1. Escolher **uma** chave e fixá-la nas três linguagens. Recomendação:
   `deployment.environment.name` (padrão atual do spec), atualizando o semconv
   do Go para uma versão >= 1.27 e usando a constante
   (`semconv.DeploymentEnvironmentName(...)`) em vez de string literal.
2. Auditar .NET (`ConfigureResource` em `TelemetryExtensions.cs:55-60` hoje nem
   emite environment como resource attribute — só o Go emite; isso é outra
   divergência a corrigir) e Python (`setup.py:42-44`, idem).
3. Atualizar as queries dos dashboards em `dashboards/` para a chave escolhida.
4. Nota: como o princípio do projeto é "resource attributes no Collector", uma
   alternativa válida é **remover** o atributo do SDK Go e deixar 100% no
   Collector — o importante é as três linguagens fazerem igual.

---

## P9 — .NET: options resolvidas duas vezes (risco de divergência com `IConfiguration`)

**Severidade: Média-Baixa (funciona hoje, mas é armadilha latente)**

### Evidência

`dotnet/OtelHelper/TelemetryExtensions.cs:44-46`:

```csharp
// Build resolved options for pipeline setup
var opts = new TelemetryOptions();
configure(opts);
new TelemetryOptionsPostConfigure().PostConfigure(null, opts);
```

Existe a cópia do pipeline de DI (`AddOptions` → `Configure` → `PostConfigure`
→ `ValidateOnStart`) e essa cópia manual criada na hora do registro. O builder
OTel é configurado com a cópia manual.

### Impacto

Se o consumidor fizer `AddOtelHelper(o => o.ServiceName = config["Svc"])` e a
configuração mudar entre o registro e a resolução (reload de `IConfiguration`,
`ConfigureOptions` registrado depois, etc.), as duas cópias divergem: a
validação valida uma coisa, o pipeline usa outra.

### Correção

Configurar dentro dos callbacks `WithTracing`/`WithMetrics` resolvendo do
container, padrão do SDK .NET moderno:

```csharp
services.AddOpenTelemetry()
    .WithTracing((sp, builder) =>
    {
        var o = sp.GetRequiredService<IOptions<TelemetryOptions>>().Value;
        if (o.IsSignalEnabled("traces")) builder.ConfigureTracing(o);
    })
    .WithMetrics((sp, builder) => { ... });
```

(Os overloads que recebem `IServiceProvider` existem desde
`OpenTelemetry.Extensions.Hosting` 1.4.) O mesmo vale para o `ActivitySource`
e `Meter` registrados como singleton em `TelemetryExtensions.cs:49-52` — usar
factory `sp => new ActivitySource(sp.GetRequiredService<IOptions<...>>().Value.ServiceName)`.

---

## Resumo priorizado

| # | Problema | Linguagem | Severidade | Esforço | Status |
|---|----------|-----------|------------|---------|--------|
| P1 | Extras no-op / core pesado | Python | Alta | Baixo | ✅ Resolvido (PR #1) |
| P2 | Pin `==` em biblioteca | Python | Alta | Baixo | ✅ Resolvido (PR #1) |
| P3 | Porta do endpoint descartada | Python | Alta | Baixo | ✅ Resolvido (PR #1) |
| P4 | `/metrics` aditivo (`OTEL_METRICS_EXPORTER`) | Todas | Feature | Médio | ✅ Resolvido (PR #2) |
| P5 | Servidor Go frágil (erro/timeout/shutdown/registry) | Go | Média-Alta | Baixo | ✅ Resolvido (PR #2) |
| P6 | Multi-worker quebra `/metrics` + handler montável | Python (Go/.NET ganham handler) | Média-Alta | Médio | ✅ Resolvido (PR #2) |
| P7 | Env vars padrão OTel ignoradas / 30s hardcoded | Todas | Média | Médio | ✅ Resolvido (PR #2) |
| P8 | Semconv `deployment.environment` divergente | Go (auditar todas) | Média | Baixo | ✅ Resolvido |
| P9 | Options duplicadas no registro | .NET | Média-Baixa | Baixo | ✅ Resolvido (com ressalva, ver seção P9) |

**PRs:**

1. **PR 1 (bugs Python):** P1 + P2 + P3 — mergeado.
2. **PR 2 (feature /metrics):** P4 + P6 + P7 — mergeado.
3. **PR 3 (site MkDocs):** infraestrutura de documentação, fora desta lista.
4. **P8 + P9:** implementados diretamente em `main` após os PRs acima
   (ver notas de resolução em cada seção).

### Nota de resolução — P8

Chave escolhida: `deployment.environment.name` (semconv ≥ v1.27), emitida
agora nas **três linguagens** (antes só o Go emitia, e com a chave legada
`deployment.environment`). Não foi feito upgrade do módulo `go.opentelemetry.io/otel`
no Go — a versão pinada (v1.31.0) só empacota semconv até v1.26.0, sem a
constante tipada; usei string literal (mesmo padrão que o código já usava),
evitando um bump de dependência fora de escopo. Nenhum dashboard em
`dashboards/*.json` referenciava a chave antiga — nada a migrar ali. Testes
adicionados/atualizados: Go (`coverage_test.go`), Python (`test_setup.py`).
.NET não tem uma forma pública simples de inspecionar o `Resource` construído
(sem `.Attributes()` como o Go) — a mudança segue o mesmo padrão já coberto
por `ResourceAttributes_Accepted_In_Pipeline`, sem teste dedicado novo.

### Nota de resolução — P9

Verifiquei empiricamente (não presumido) que `OpenTelemetry.Extensions.Hosting`
1.15.3 **não tem** overloads sensíveis a `IServiceProvider` para
`ConfigureResource`/`WithTracing`/`WithMetrics` — a correção sugerida
originalmente (usar esses overloads) não compila contra o SDK instalado.
Correção aplicada em vez disso:

- `ActivitySource`/`Meter`: registrados como singleton via **factory lazy**
  (`services.AddSingleton(sp => new ActivitySource(sp.GetRequiredService<IOptions<TelemetryOptions>>().Value.ServiceName))`)
  — resolvido de verdade na primeira resolução via DI, sem cópia manual.
  Totalmente corrigido, sem ressalva.
- Resource/Tracing/Metrics/Logging: a cópia manual duplicada
  (`new TelemetryOptions(); configure(opts); new TelemetryOptionsPostConfigure().PostConfigure(...)`)
  foi **removida** e substituída por resolução através do pipeline real de
  options via um `ServiceProvider` provisório (`services.BuildServiceProvider()`),
  eliminando a segunda implementação hand-rolled que podia divergir da real.

**Ressalva documentada em código:** como não existe overload sensível a `sp`
nesses pontos do SDK, a resolução ainda acontece uma vez, de forma síncrona,
durante `AddOtelHelper()` — um `services.Configure<TelemetryOptions>(...)`
registrado pelo consumidor **depois** dessa chamada não é refletido no
pipeline de tracing/metrics/resource (embora seja refletido por qualquer
`IOptions<TelemetryOptions>` resolvido depois, incluindo o `ValidateOnStart`).
Resolver isso por completo exigiria reescrever `ConfigureTracing`/`ConfigureMetrics`
para construir sampler/exporter/processor via os overloads `Func<IServiceProvider,T>`
de baixo nível que o SDK oferece (`SetSampler(Func<...>)`, `AddProcessor(Func<...>)`,
`AddReader(Func<...>)`) — escopo maior, não justificado pela severidade
Média-Baixa original do problema.

**Efeito colateral (bom) descoberto durante o teste:** validação de config
inválida agora falha **dentro de `AddOtelHelper()`** (fail-fast), não mais
apenas quando algo resolve `IOptions<TelemetryOptions>.Value` depois (ex.: o
hosted service do `ValidateOnStart`, no startup real da app). Dois testes
que assumiam o comportamento antigo (`ValidateOnStart_Throws_With_Empty_ServiceName`,
`ValidateOnStart_Throws_With_Invalid_Endpoint`) foram atualizados para refletir
isso.
