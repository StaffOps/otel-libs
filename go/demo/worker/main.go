// Command worker is the Go demo worker (specs/demo-environment DE-D2, DE-D5,
// DE-D6, DE-D7): it drives the fixed scenario cycle against the api (one
// POST /orders per interval, each in a new root span demo.drive) and consumes
// the order events of the Redis stream demo:go:orders (one CONSUMER span per
// message, with the producer's context as parent and as its one link).
//
// Environment (besides the OTEL_* variables): DEMO_API_URL
// (http://demo-go-api:8080), DEMO_REDIS_ADDR (demo-redis:6379),
// DEMO_REDIS_PASSWORD_FILE (a mounted file, DE-D13), DEMO_INTERVAL_SECONDS
// (5), DEMO_POLL_SECONDS (2).
package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"github.com/redis/go-redis/v9/maintnotifications"
	otelhelper "github.com/staffops/otel-libs/go"
	"github.com/staffops/otel-libs/go/demo/internal/demokit"
	"github.com/staffops/otel-libs/go/ext/otelredis"
)

func main() {
	if err := run(); err != nil {
		log.Printf("demo-go-worker: %v", err)
		os.Exit(1)
	}
}

func run() error {
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()

	logger, flush, err := demokit.Setup(ctx)
	if err != nil {
		return err
	}
	defer flush()

	password, err := demokit.ReadSecretFile("DEMO_REDIS_PASSWORD_FILE")
	if err != nil {
		return err
	}
	redisAddr := demokit.EnvOr("DEMO_REDIS_ADDR", "demo-redis:6379")
	// Maintenance notifications off: go-redis v9.23 otherwise sends CLIENT
	// MAINT_NOTIFICATIONS ON per new connection, which Redis 7.4 rejects with
	// ERR, leaving an ERROR span per connection (DE3a smoke, 2026-11-16).
	rdb := redis.NewClient(&redis.Options{
		Addr:                     redisAddr,
		Password:                 password,
		MaintNotificationsConfig: &maintnotifications.Config{Mode: maintnotifications.ModeDisabled},
	})
	defer func() { _ = rdb.Close() }()
	if err := otelredis.Instrument(rdb); err != nil {
		return fmt.Errorf("instrument redis: %w", err)
	}

	consumer, _ := os.Hostname()
	if consumer == "" {
		consumer = "worker"
	}
	host, port := demokit.HostPort(redisAddr, 6379)
	w := &worker{
		logger:    logger,
		tracer:    otelhelper.GetTracer(demokit.TracerName),
		http:      &http.Client{Transport: otelhelper.NewHTTPTransport(nil), Timeout: 10 * time.Second},
		apiURL:    strings.TrimRight(demokit.EnvOr("DEMO_API_URL", "http://demo-"+demokit.Lang+"-api:8080"), "/"),
		stream:    redisStream{rdb: rdb, consumer: consumer},
		redisHost: host,
		redisPort: port,
	}

	if err := w.ensureGroup(ctx); err != nil {
		return err
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		every(ctx, demokit.EnvSeconds("DEMO_INTERVAL_SECONDS", 5*time.Second), func(seq uint64) { w.drive(ctx, seq) })
	}()
	go func() {
		defer wg.Done()
		every(ctx, demokit.EnvSeconds("DEMO_POLL_SECONDS", 2*time.Second), func(uint64) { w.poll(ctx) })
	}()
	logger.Info("demo worker started", "messaging.destination.name", demokit.StreamKey)
	wg.Wait()
	return nil
}

// every calls fn with 1, 2, 3, ... once per interval until ctx is done.
func every(ctx context.Context, interval time.Duration, fn func(seq uint64)) {
	t := time.NewTicker(interval)
	defer t.Stop()
	var seq uint64
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			seq++
			fn(seq)
		}
	}
}

// ensureGroup creates the consumer group (and the stream), waiting for Redis
// for up to a minute; an existing group is fine.
func (w *worker) ensureGroup(ctx context.Context) error {
	var err error
	for attempt := 1; attempt <= 30; attempt++ {
		if err = w.stream.CreateGroup(ctx); err == nil {
			return nil
		}
		w.logger.Warn("consumer group not ready", "attempt", attempt, "error", err.Error())
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(2 * time.Second):
		}
	}
	return fmt.Errorf("create consumer group: %w", err)
}

// redisStream is the queue side of the worker on the instrumented client.
type redisStream struct {
	rdb      *redis.Client
	consumer string
}

// CreateGroup makes sure the consumer group exists, created at "$" with
// MKSTREAM (DE-D7 r3.5). It looks first (TYPE, then XINFO GROUPS) so an
// existing group (BUSYGROUP) or a missing stream (XINFO's "no such key") never
// costs a failing command, which would be an ERROR span. A BUSYGROUP from a
// concurrent creator is still accepted.
func (s redisStream) CreateGroup(ctx context.Context) error {
	typ, err := s.rdb.Type(ctx, demokit.StreamKey).Result()
	if err != nil {
		return err
	}
	if typ == "stream" {
		groups, err := s.rdb.XInfoGroups(ctx, demokit.StreamKey).Result()
		if err != nil {
			return err
		}
		for _, g := range groups {
			if g.Name == demokit.ConsumerGroup {
				return nil
			}
		}
	}
	err = s.rdb.XGroupCreateMkStream(ctx, demokit.StreamKey, demokit.ConsumerGroup, "$").Err()
	if err != nil && strings.HasPrefix(err.Error(), "BUSYGROUP") {
		return nil
	}
	return err
}

// Read is XREADGROUP COUNT 10 without BLOCK (DE-D5). go-redis adds BLOCK for
// any Block >= 0 (BLOCK 0 waits forever), so Block is -1.
func (s redisStream) Read(ctx context.Context) ([]message, error) {
	res, err := s.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
		Group:    demokit.ConsumerGroup,
		Consumer: s.consumer,
		Streams:  []string{demokit.StreamKey, ">"},
		Count:    10,
		Block:    -1,
	}).Result()
	if errors.Is(err, redis.Nil) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []message
	for _, st := range res {
		for _, m := range st.Messages {
			out = append(out, message{ID: m.ID, Values: m.Values})
		}
	}
	return out, nil
}

func (s redisStream) Ack(ctx context.Context, id string) error {
	return s.rdb.XAck(ctx, demokit.StreamKey, demokit.ConsumerGroup, id).Err()
}
