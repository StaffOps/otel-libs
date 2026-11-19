// Package demokit holds what the three Go demo apps (worker, api, backend)
// share: the names of the demo contract, the scenario cycle, the pinned error
// types and the ADR-014 failure pattern (specs/demo-environment DE-D2, DE-D5,
// DE-D7, DE-D8, DE-D9).
package demokit

import (
	"fmt"
	"net"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"
)

// Names fixed by the demo contract. Every language uses the same values with
// its own <lang>; the Go apps are <lang> = "go".
const (
	// Lang is the <lang> part of service names, Redis keys and the database.
	Lang = "go"
	// TracerName is the instrumentation scope of every demo span and
	// instrument (check d selects demo spans by it, DE-D8).
	TracerName = "otel-libs-demo"
	// StreamKey is the per-language Redis stream used as the queue (DE-D5).
	StreamKey = "demo:" + Lang + ":orders"
	// ConsumerGroup is the stream's consumer group (DE-D5).
	ConsumerGroup = "demo-workers"
	// QuoteTTL is the lifetime of a cached quote (DE-D4).
	QuoteTTL = 30 * time.Second
	// StreamMaxLen caps the stream (XADD MAXLEN ~ 1000, DE-D4).
	StreamMaxLen = 1000
	// MaxOrderAmount is the backend's business rule: a larger amount is
	// rejected with OrderRejectedError (business_error scenario).
	MaxOrderAmount = 1000.0
	// okIDSpace bounds the rows an ok order can create (ok-<seq mod 500>, DE-D4).
	okIDSpace = 500
)

// Scenario names (design "Scenario table").
const (
	ScenarioOK            = "ok"
	ScenarioBusinessError = "business_error"
	ScenarioDBError       = "db_error"
	ScenarioUnhandled     = "unhandled"
)

// cycle is the fixed sequence of ten requests (DE-D7): ok at 1, 2, 4, 5, 7,
// 8, 10; business_error at 3; db_error at 6; unhandled at 9.
var cycle = [10]string{
	ScenarioOK, ScenarioOK, ScenarioBusinessError, ScenarioOK, ScenarioOK,
	ScenarioDBError, ScenarioOK, ScenarioOK, ScenarioUnhandled, ScenarioOK,
}

// customers cycle over three values so the quote cache both hits and misses.
var customers = [3]string{"cust-a", "cust-b", "cust-c"}

// QuoteKey is the cache key of one customer's quote (DE-D4).
func QuoteKey(customer string) string { return "demo:" + Lang + ":quote:" + customer }

// ScenarioAt returns the scenario of the seq-th request, seq counted from 1.
// seq 0 is treated as position 10, so the function is total.
func ScenarioAt(seq uint64) string {
	return cycle[(seq+9)%10]
}

// ValidScenario reports whether s is one of the four scenarios. Only these
// values ever become a span or metric attribute (bounded cardinality).
func ValidScenario(s string) bool {
	switch s {
	case ScenarioOK, ScenarioBusinessError, ScenarioDBError, ScenarioUnhandled:
		return true
	}
	return false
}

// OrderRequest is the worker's HTTP body (DE-D7) and the api's input.
type OrderRequest struct {
	OrderID  string  `json:"order_id"`
	Customer string  `json:"customer"`
	Amount   float64 `json:"amount"`
	Scenario string  `json:"scenario"`
}

// OrderFor builds the body of the seq-th request. Order ids never carry a run
// token: order.id is a span attribute, and a run token on a span is a hygiene
// finding (DR12). ok orders use ok-<seq mod 500> so the table stays bounded;
// business_error sends an amount over MaxOrderAmount.
func OrderFor(seq uint64) OrderRequest {
	scenario := ScenarioAt(seq)
	n := seq % okIDSpace
	o := OrderRequest{
		OrderID:  "ord-" + strconv.FormatUint(n, 10),
		Customer: customers[seq%uint64(len(customers))],
		Amount:   20,
		Scenario: scenario,
	}
	switch scenario {
	case ScenarioOK:
		o.OrderID = "ok-" + strconv.FormatUint(n, 10)
		o.Amount = float64(10 + seq%90)
	case ScenarioBusinessError:
		o.Amount = 5 * MaxOrderAmount
	}
	return o
}

// DuplicateID is the row the db_error scenario inserts; the schema's trigger
// rejects every dup- id with SQLSTATE 23505 (DE-D4).
func DuplicateID(run string) string { return "dup-" + run }

// EnvOr returns the trimmed value of key, or def when unset or blank.
func EnvOr(key, def string) string {
	if v := strings.TrimSpace(os.Getenv(key)); v != "" {
		return v
	}
	return def
}

// EnvSeconds reads a positive number of seconds (fractions allowed) from key;
// unset, blank or invalid gives def.
func EnvSeconds(key string, def time.Duration) time.Duration {
	v := strings.TrimSpace(os.Getenv(key))
	if v == "" {
		return def
	}
	f, err := strconv.ParseFloat(v, 64)
	if err != nil || f <= 0 || f > 3600 {
		return def
	}
	return time.Duration(f * float64(time.Second))
}

// ReadSecretFile returns the content of a mounted secret file without
// surrounding whitespace (DE-D13: passwords come from files, never from env
// values). Errors name the variable and path, never the content.
func ReadSecretFile(envKey string) (string, error) {
	path := strings.TrimSpace(os.Getenv(envKey))
	if path == "" {
		return "", fmt.Errorf("%s is not set", envKey)
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read %s (%s): %w", envKey, path, err)
	}
	s := strings.TrimSpace(string(b))
	if s == "" {
		return "", fmt.Errorf("%s (%s) is empty", envKey, path)
	}
	return s, nil
}

// RunID returns the run id the apps put in their run tokens (rej-<run>,
// dup-<run>, unh-<run>): the otel_libs.run entry of OTEL_RESOURCE_ATTRIBUTES,
// the one place the run is configured (DR0, DE-D2), or "local" when absent.
// Values are percent-decoded as the OTel resource attribute format requires.
func RunID() string {
	for _, kv := range strings.Split(os.Getenv("OTEL_RESOURCE_ATTRIBUTES"), ",") {
		k, v, ok := strings.Cut(kv, "=")
		if !ok || strings.TrimSpace(k) != "otel_libs.run" {
			continue
		}
		v = strings.TrimSpace(v)
		if dec, err := url.PathUnescape(v); err == nil {
			v = dec
		}
		if v != "" {
			return v
		}
	}
	return "local"
}

// HostPort splits addr into server.address and server.port; a missing or
// invalid port gives defPort.
func HostPort(addr string, defPort int) (string, int) {
	host, p, err := net.SplitHostPort(addr)
	if err != nil {
		return addr, defPort
	}
	port, err := strconv.Atoi(p)
	if err != nil || port <= 0 || port > 65535 {
		return host, defPort
	}
	return host, port
}
