# EXAMPLES.md — Detailed Check and Simulation Descriptions

This document describes each check/simulation implemented in the samples, its objective, how it works internally, and how to query results in Grafana.

---

## SampleApi — Endpoints

### 1. `GET /order/{id}` — Trace distribuída completa

**Objetivo:** Demonstrar propagação de contexto entre 3 serviços via gRPC.

**Fluxo:**
1. API cria span `api.get-order` com tag `order.id`
2. Chama Backend via gRPC `ProcessOrder`
3. Backend cria spans `backend.db-query-order` (10-80ms) + `backend.external-enrich-order` (httpbin ~1s)
4. Resposta retorna com dados do pedido

**Consultar no Grafana:**
```
# Tempo — trace completa
{resource.service.name="sample-api" && name="api.get-order"}

# Ver os 3 serviços no mesmo trace
{resource.service.name=~"sample-api|sample-backend|sample-process" && name="api.get-order"}
```

---

### 2. `POST /order` — Criação com body JSON

**Objetivo:** Demonstrar instrumentação de endpoint POST com body parsing.

**Fluxo:**
1. Lê body `{"product": "...", "quantity": N}`
2. Gera ID aleatório
3. Chama Backend via gRPC `ProcessOrder`
4. Retorna 201 com dados

**Consultar no Grafana:**
```
{resource.service.name="sample-api" && name="api.create-order"}
```

---

### 3. `GET /order/{id}/cancel` — Cancelamento

**Objetivo:** Demonstrar operação de escrita (UPDATE) no Backend.

**Fluxo:**
1. API cria span `api.cancel-order`
2. Chama Backend via gRPC `CancelOrder`
3. Backend cria span `backend.db-cancel-order` (5-30ms)

**Consultar no Grafana:**
```
{resource.service.name="sample-api" && name="api.cancel-order"}
```

---

### 4. `GET /slow` — Operação lenta

**Objetivo:** Demonstrar detecção de alta latência pelo tail sampling (traces >1s sempre mantidos).

**Fluxo:**
1. API cria span `api.slow-operation`
2. Chama Backend via gRPC `SlowOperation`
3. Backend: `backend.db-heavy-query` (3-5s) + `backend.external-slow-enrichment` (1-2s)
4. Total: 4-7s

**Consultar no Grafana:**
```
# Traces lentas
{resource.service.name="sample-api" && duration > 3s}

# Especificamente o slow
{resource.service.name="sample-api" && name="api.slow-operation"}
```

---

### 5. `GET /batch` — Fan-out sequencial

**Objetivo:** Demonstrar trace com muitos spans (fan-out pattern).

**Fluxo:**
1. API cria span `api.batch-orders`
2. Gera 5 IDs aleatórios
3. Para cada ID, cria span `api.batch-item` e chama Backend `ProcessOrder` sequencialmente
4. Total: ~37 spans no trace (5 × backend spans)

**Consultar no Grafana:**
```
{resource.service.name="sample-api" && name="api.batch-orders"}
```

---

### 6. `GET /error` — Exception simulada

**Objetivo:** Demonstrar que traces com erro são sempre mantidos pelo sampling (100% em qualquer ambiente).

**Fluxo:**
1. API cria span `api.error-simulated` com `StatusCode.Error`
2. Lança `InvalidOperationException`
3. Retorna 500

**Consultar no Grafana:**
```
# Traces com erro
{resource.service.name="sample-api" && status=error}
```

---

### 7. `GET /health/ready` — Readiness probe

**Objetivo:** Demonstrar health check que valida dependência (Backend).

**Fluxo:**
1. API cria span `api.readiness-check`
2. Chama Backend via gRPC `ProcessOrder` com ID 0
3. Se sucesso → 200, se falha → 503

**Consultar no Grafana:**
```
{resource.service.name="sample-api" && name="api.readiness-check"}
```

---

### 8. `GET /order/{id}/trace` — Baggage propagation

**Objetivo:** Demonstrar propagação de metadados (baggage) entre serviços sem adicionar ao span diretamente. Também demonstra scoped logging.

**Como funciona:**
1. API seta 3 baggage items: `tenant.id`, `order.id`, `feature.flag`
2. Usa `logger.BeginScope(...)` para adicionar contexto ao log
3. Chama Backend via gRPC `ReadBaggage`
4. Backend lê `Baggage.Current` e retorna os items recebidos
5. Baggage é propagado automaticamente via headers gRPC (sem código extra)

**O que valida:**
- Baggage propaga entre serviços automaticamente
- Backend recebe os 3 items sem configuração adicional
- Scoped logging adiciona `TenantId` e `OrderId` aos logs

**Consultar no Grafana:**
```
# Tempo
{resource.service.name="sample-api" && name="api.trace-with-baggage"}

# Loki — ver baggage nos logs do Backend
{service_name="sample-backend"} |= "ReadBaggage"
```

---

### 9. `GET /order/{id}/events` — Span Events

**Objetivo:** Demonstrar eventos dentro de um span (pontos no tempo, sem criar sub-spans).

**Como funciona:**
1. API cria span `api.order-with-events`
2. Adiciona 4 events ao span:
   - `order.received` (com tags: order.id, order.source)
   - `order.validated` (após delay de validação)
   - `order.enriched` (após chamada ao Backend)
   - `order.completed` (com tag: status)
3. Events aparecem como timeline dentro do span no Grafana

**O que valida:**
- Events são visíveis no detalhe do span no Tempo
- Cada event tem timestamp próprio (mostra duração entre etapas)
- Tags nos events adicionam contexto sem poluir o span

**Consultar no Grafana:**
```
# Tempo — abrir o span e ver a aba "Events"
{resource.service.name="sample-api" && name="api.order-with-events"}
```

---

### 10. `GET /parallel/{count}` — Parallel fan-out

**Objetivo:** Demonstrar spans concorrentes (paralelos) dentro de um trace.

**Como funciona:**
1. API cria span `api.parallel-fan-out` com tag `fan_out.count`
2. Dispara N chamadas ao Backend em paralelo via `Task.WhenAll`
3. Cada chamada cria span `api.parallel-item` (children concorrentes)
4. No Grafana, os spans aparecem lado a lado (não sequenciais)

**O que valida:**
- Spans paralelos são visualizados corretamente no Tempo
- Duração total ≈ duração do item mais lento (não soma)
- Cada item tem seu próprio span com tags

**Consultar no Grafana:**
```
{resource.service.name="sample-api" && name="api.parallel-fan-out"}
```

---

### 11. `GET /retry/{id}` — Retry com spans por tentativa

**Objetivo:** Demonstrar padrão de retry onde cada tentativa é um span separado, com ERROR nas falhas e SUCCESS na última.

**Como funciona:**
1. API cria span pai `api.retry-operation`
2. Loop de até 3 tentativas, cada uma com span `api.retry-attempt`
3. Chama Backend `UnstableOperation` (configurado para falhar 2x)
4. Tentativas 1 e 2: span com `StatusCode.Error` + log warning
5. Tentativa 3: sucesso
6. Backoff exponencial entre tentativas (100ms, 200ms)

**O que valida:**
- Cada tentativa é visível como span separado no trace
- Spans de falha têm status ERROR (ícone vermelho no Grafana)
- O span pai mostra a operação completa
- Log com exception stack trace correlacionado ao trace

**Consultar no Grafana:**
```
# Trace com retries
{resource.service.name="sample-api" && name="api.retry-operation"}

# Só as falhas
{resource.service.name="sample-api" && name="api.retry-attempt" && status=error}
```

---

### 12. `GET /cache/{id}` — Cache hit/miss

**Objetivo:** Demonstrar padrão de cache com métricas de hit rate e spans diferenciados.

**Como funciona:**
1. API cria span `api.cache-lookup` com tag `order.id`
2. Verifica cache in-memory (ConcurrentDictionary)
3. **HIT**: tag `cache.hit=true`, incrementa `cache.hits_total`, retorna imediato
4. **MISS**: tag `cache.hit=false`, incrementa `cache.misses_total`, chama Backend, armazena resultado

**O que valida:**
- Primeira chamada: MISS (span longo, chama Backend)
- Segunda chamada mesmo ID: HIT (span curto, sem Backend)
- Métricas `cache.hits_total` e `cache.misses_total` exportadas
- Tag `cache.hit` no span permite filtrar no Tempo

**Consultar no Grafana:**
```
# Tempo — filtrar por hit/miss
{resource.service.name="sample-api" && name="api.cache-lookup" && span.cache.hit=true}

# PromQL — cache hit rate
rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))
```

---

## SampleBackend — RPCs

### `UnstableOperation`

**Objetivo:** Servir como target para o endpoint `/retry` da API. Simula serviço instável.

**Como funciona:**
- Mantém contador global de chamadas
- Falha nas primeiras N chamadas (retorna `StatusCode.Unavailable`)
- Sucesso a cada N+1 chamadas
- Span `backend.unstable-operation` com tag `attempt`

---

### `ReadBaggage`

**Objetivo:** Demonstrar que baggage propagado via gRPC é acessível no serviço downstream.

**Como funciona:**
- Lê `Baggage.Current` (populado automaticamente pelo OTel SDK via headers gRPC)
- Retorna todos os items como mapa key→value
- Adiciona cada item como tag no span

---

## SampleProcess — Workers

### ApiHealthWorker (a cada 1 minuto)

**Objetivo:** Gerar traces distribuídas continuamente, validando a comunicação entre os 3 serviços.

**Como funciona:**
- 6 chamadas sequenciais à API, cada uma com `StartRootActivity` (trace independente)
- Span names descritivos por endpoint:
  - `process-api-check` → `GET /`
  - `process-api-order` → `GET /order/1`
  - `process-api-cancel` → `GET /order/1/cancel`
  - `process-api-health-ready` → `GET /health/ready`
  - `process-api-batch` → `GET /batch`
  - `process-api-simulate-error` → `GET /error`

**Métricas:**
- `api_health.checks_total` (por endpoint)
- `api_health.checks_failed_total` (por endpoint)
- `api_health.check_duration_seconds` (por endpoint)

**Consultar no Grafana:**
```
# Tempo — todos os health checks
{resource.service.name="sample-process" && name=~"process-api-.*"}

# PromQL — success rate
1 - (rate(api_health_checks_failed_total[1h]) / rate(api_health_checks_total[1h]))
```

---

### HeavyProcessWorker (a cada 2 minutos)

**Objetivo:** Gerar carga de CPU e memória para observar no runtime metrics (.NET GC, thread pool).

**Como funciona:**
- Batch de 8-20 itens em paralelo (`StartRootActivity`)
- Cada item: SHA256 ×2000 (string 50KB) + 500 buffers (4-32KB)
- 5% de chance de falha simulada por item
- Span `process.heavy-batch` (root) → N× `process.heavy-process-item` (children paralelos)

**Métricas:**
- `heavy_work.batches_total`
- `heavy_work.items_total` (tag: status)
- `heavy_work.errors_total`
- `heavy_work.batch_duration_seconds`
- `heavy_work.item_duration_seconds`
- `heavy_work.items_active` (gauge)

**Consultar no Grafana:**
```
# Tempo
{resource.service.name="sample-process" && name="process.heavy-batch"}

# PromQL — throughput
rate(heavy_work_items_total[5m])

# PromQL — error rate
rate(heavy_work_errors_total[5m]) / rate(heavy_work_items_total[5m])

# Runtime — GC pressure (correlacionar com batch execution)
dotnet_gc_collections_total
```

---

### QueueConsumerWorker (a cada 30 segundos)

**Objetivo:** Demonstrar o padrão de instrumentação para consumers de fila (SQS, Kafka, RabbitMQ).

**Como funciona:**
- Simula poll de fila: gera 1-5 "mensagens" fictícias por ciclo
- Cada mensagem é um trace independente (`StartRootActivity`)
- Dentro do trace, 3 etapas: parse → validate → persist
- 10% de chance de falha na validação (span com ERROR)
- Observable gauge `queue.depth` mostra profundidade da fila

**Span hierarchy por mensagem:**
```
process.queue-consume (root)
  ├── process.queue-parse
  ├── process.queue-validate
  └── process.queue-persist
```

**Tags no span root:**
- `messaging.message_id` — ID único da mensagem
- `messaging.system` — "sqs"
- `messaging.destination` — "orders-queue"

**Métricas:**
- `queue.messages_processed_total` (tag: status)
- `queue.messages_failed_total`
- `queue.message_duration_seconds`
- `queue.depth` (observable gauge)

**Consultar no Grafana:**
```
# Tempo — traces de mensagens
{resource.service.name="sample-process" && name="process.queue-consume"}

# Tempo — só falhas de validação
{resource.service.name="sample-process" && name="process.queue-validate" && status=error}

# PromQL — throughput de mensagens
rate(queue_messages_processed_total[5m])

# PromQL — queue depth
queue_depth

# PromQL — failure rate
rate(queue_messages_failed_total[5m]) / rate(queue_messages_processed_total[5m])
```

---

### ScheduledJobWorker (a cada 3 minutos)

**Objetivo:** Demonstrar job agendado com timeout, detecção de falha, e circuit breaker.

**Como funciona:**
1. Job executa 3 etapas: fetch-data → transform → persist
2. `fetch-data` tem delay aleatório (100ms-7s) — às vezes excede o timeout de 5s
3. Se timeout: span com ERROR + tag `timeout=true`
4. Após 3 falhas consecutivas: circuit breaker abre
5. Circuit aberto: job é rejeitado (span com tag `circuit_breaker.action=rejected`)
6. Após 30s: circuit vai para half-open, tenta novamente
7. Se sucesso: circuit fecha, contador reseta

**Span hierarchy (sucesso):**
```
process.scheduled-job (root)
  ├── process.job-fetch-data
  ├── process.job-transform
  └── process.job-persist
```

**Span hierarchy (timeout):**
```
process.scheduled-job (root, ERROR, timeout=true)
  └── process.job-fetch-data (cancelado pelo timeout)
```

**Estados do circuit breaker:**
- `0` = closed (normal, jobs executam)
- `1` = open (jobs rejeitados, aguardando recovery)
- `2` = half-open (tentando recovery)

**Métricas:**
- `scheduled_job.runs_total` (tag: result = success/timeout/error/circuit-open)
- `scheduled_job.timeouts_total`
- `scheduled_job.duration_seconds`
- `circuit_breaker.state` (observable gauge: 0/1/2)

**Consultar no Grafana:**
```
# Tempo — todos os jobs
{resource.service.name="sample-process" && name="process.scheduled-job"}

# Tempo — só timeouts
{resource.service.name="sample-process" && name="process.scheduled-job" && span.timeout=true}

# PromQL — timeout rate
rate(scheduled_job_timeouts_total[1h]) / rate(scheduled_job_runs_total[1h])

# PromQL — circuit breaker state
circuit_breaker_state

# PromQL — job duration P95
histogram_quantile(0.95, rate(scheduled_job_duration_seconds_bucket[5m]))
```

---

## Resumo de funcionalidades demonstradas

| Funcionalidade | Onde | Span/Métrica |
|---|---|---|
| Trace distribuída (3 serviços) | API `/order/{id}` | `api.get-order` |
| Baggage propagation | API `/order/{id}/trace` | `api.trace-with-baggage` |
| Span events | API `/order/{id}/events` | `api.order-with-events` |
| Parallel fan-out | API `/parallel/{count}` | `api.parallel-fan-out` |
| Retry com spans | API `/retry/{id}` | `api.retry-operation` |
| Cache hit/miss | API `/cache/{id}` | `api.cache-lookup` + `cache.hits_total` |
| Error trace (sempre mantido) | API `/error` | `api.error-simulated` |
| High latency (sempre mantido) | API `/slow` | `api.slow-operation` |
| gRPC instrumentation | API → Backend | Auto-spans gRPC client/server |
| StartRootActivity (workers) | Process todos | Traces independentes por iteração |
| Observable gauge | Process queue/circuit | `queue.depth`, `circuit_breaker.state` |
| Consumer pattern | Process queue | `process.queue-consume` |
| Timeout detection | Process job | `process.scheduled-job` + `timeout=true` |
| Circuit breaker | Process job | `circuit_breaker.state` gauge |
| CPU/memory stress | Process heavy | `process.heavy-batch` |
| Scoped logging | API `/order/{id}/trace` | `logger.BeginScope(...)` |
| RecordException | Todos (automático) | Exception stack trace no span |
| Debug tracestate | Todos (quando debug=true) | `tracestate: debug=true` |

---

## Dashboards Grafana

3 dashboards prontos em `example/dashboards/`. Para importar: **Dashboards → Import → Upload JSON**.

| Arquivo | Título | O que mostra |
|---|---|---|
| `01-api-business-metrics.json` | Sample — API & Business Metrics | Request rate, error rate, P95 latency, cache hit/miss, revenue, DB/external duration, traces recentes |
| `02-workers-background.json` | Sample — Workers & Background Processing | Health check success rate, heavy work throughput, queue depth, circuit breaker state, scheduled job timeouts |
| `03-traces-reliability.json` | Sample — Traces & Reliability Patterns | Retry attempts, parallel fan-out, baggage propagation, span events, timeout traces, queue consumer, all errors |

**Datasources necessários:**
- `victoriametrics` (type: prometheus) — métricas
- `tempo` (type: tempo) — traces

**Variável de filtro** (dashboard 01): `$service` — seleciona entre `sample-api`, `sample-backend`, `sample-process`.
