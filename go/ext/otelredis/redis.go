// Package otelredis provides OpenTelemetry instrumentation for go-redis v9.
//
// Usage:
//
//	rdb := redis.NewClient(&redis.Options{Addr: "localhost:6379"})
//	if err := otelredis.Instrument(rdb); err != nil { ... }
//	// All subsequent Redis commands will produce trace spans.
package otelredis

import (
	"github.com/redis/go-redis/extra/redisotel/v9"
	"github.com/redis/go-redis/v9"
)

// Instrument adds OpenTelemetry tracing hooks to a go-redis v9 client.
// Accepts redis.Client, redis.ClusterClient, or any redis.UniversalClient.
func Instrument(client redis.UniversalClient) error {
	return redisotel.InstrumentTracing(client)
}
