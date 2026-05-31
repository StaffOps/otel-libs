package otelhelper

import (
	"context"
	"testing"
)

func resetGlobals() {
	mu.Lock()
	defer mu.Unlock()
	setupDone = false
	shutdownFn = nil
	setupErr = nil
}

func TestSetupValidConfig(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "test-svc")

	shutdown, err := Setup(context.Background(), WithEndpoint("localhost:4317"))
	if err != nil {
		t.Fatalf("Setup failed: %v", err)
	}
	if shutdown == nil {
		t.Fatal("Shutdown should not be nil")
	}
	ctx := context.Background()
	if err := shutdown(ctx); err != nil {
		t.Logf("Shutdown error (expected with no collector): %v", err)
	}
}

func TestSetupInvalidConfig(t *testing.T) {
	resetGlobals()

	shutdown, err := Setup(context.Background(), WithServiceName("  "))
	if err == nil {
		t.Fatal("Expected error for blank service name")
	}
	if shutdown == nil {
		t.Fatal("Shutdown should be non-nil (no-op) even on error")
	}
	// no-op shutdown should not panic
	if err := shutdown(context.Background()); err != nil {
		t.Fatalf("no-op shutdown should not error: %v", err)
	}
}

func TestSetupRetryAfterValidationFailure(t *testing.T) {
	resetGlobals()

	// First call with invalid config
	_, err := Setup(context.Background(), WithServiceName("  "), WithEndpoint("localhost:4317"))
	if err == nil {
		t.Fatal("Expected error for blank service name")
	}

	// Second call with valid config should succeed (not permanently bricked)
	t.Setenv(EnvServiceName, "retry-svc")
	shutdown, err := Setup(context.Background(), WithServiceName("retry-svc"), WithEndpoint("localhost:4317"))
	if err != nil {
		t.Fatalf("Second Setup should succeed after validation fix: %v", err)
	}
	if shutdown == nil {
		t.Fatal("Shutdown should not be nil on success")
	}
}

func TestSetupDoubleInit(t *testing.T) {
	resetGlobals()
	t.Setenv(EnvServiceName, "test-svc")

	s1, err1 := Setup(context.Background(), WithEndpoint("localhost:4317"))
	if err1 != nil {
		t.Fatalf("First Setup failed: %v", err1)
	}

	s2, err2 := Setup(context.Background(), WithEndpoint("other:4317"))
	if err2 != nil {
		t.Fatalf("Second Setup failed: %v", err2)
	}

	if s1 == nil || s2 == nil {
		t.Fatal("Both shutdowns should be non-nil")
	}
}
