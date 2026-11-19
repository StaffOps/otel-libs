package otelhelper

import (
	"context"
	"errors"
	"testing"
	"time"

	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/stats"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
)

// Task A37 (ADR-014 decision 4, design D15 amendment 2026-11-19): otelgrpc
// spans carry no status description — neither the handler's text on the
// server nor the peer's status message on the client — while the status
// code, error.type and rpc.response.status_code stay. No record: Go has no
// exception object (owner decision 7).
func TestGRPCSpansCarryNoStatusMessage(t *testing.T) {
	tests := []struct {
		method       string
		client       otelcodes.Code
		server       otelcodes.Code
		clientStatus string // rpc.response.status_code
	}{
		{"Internal", otelcodes.Error, otelcodes.Error, "INTERNAL"},
		{"Plain", otelcodes.Error, otelcodes.Error, "UNKNOWN"},
		{"FailedPrecondition", otelcodes.Error, otelcodes.Unset, "FAILED_PRECONDITION"},
		{"NotFound", otelcodes.Error, otelcodes.Unset, "NOT_FOUND"},
		{"Code99", otelcodes.Error, otelcodes.Unset, "CODE(99)"},
		{"Ok", otelcodes.Unset, otelcodes.Unset, "OK"},
	}
	for _, tt := range tests {
		t.Run(tt.method, func(t *testing.T) {
			exp := inMemoryTracing(t)
			conn := errorGRPCServer(t)
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			err := conn.Invoke(ctx, "/t.v1.S/"+tt.method, &emptypb.Empty{}, &emptypb.Empty{})
			if tt.method != "Ok" && status.Convert(err).Message() == "" {
				t.Fatalf("the caller still gets the message: %v", err)
			}
			for _, c := range []struct {
				kind trace.SpanKind
				want otelcodes.Code
			}{{trace.SpanKindClient, tt.client}, {trace.SpanKindServer, tt.server}} {
				s := spanOfKind(t, exp, c.kind, "t.v1.S/"+tt.method)
				if s.Status.Code != c.want || s.Status.Description != "" {
					t.Errorf("%v status = %v %q, want %v without a description", c.kind, s.Status.Code, s.Status.Description, c.want)
				}
				if got, ok := spanAttr(s, "rpc.response.status_code"); !ok || got != tt.clientStatus {
					t.Errorf("%v rpc.response.status_code = %q (%v), want %q", c.kind, got, ok, tt.clientStatus)
				}
				if len(s.Events) != 0 {
					t.Errorf("%v events = %v, want none", c.kind, s.Events)
				}
			}
		})
	}
}

// The application's own SetStatus on the RPC span keeps its description
// when the RPC ends OK (or with a code otelgrpc leaves Unset): only otelgrpc's
// End is rewritten, no span wrapper is put in the context. On a failure
// otelgrpc's own SetStatus replaces it, now with an empty description
// (TestGRPCApplicationStatusIsReplacedOnFailure).
func TestGRPCApplicationStatusOnTheRPCSpanIsKept(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := errorGRPCServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.Invoke(ctx, "/t.v1.S/AppStatus", &emptypb.Empty{}, &emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	s := spanOfKind(t, exp, trace.SpanKindServer, "t.v1.S/AppStatus")
	if s.Status.Code != otelcodes.Error || s.Status.Description != "app text" {
		t.Errorf("server status = %v %q, want the application's Error \"app text\"", s.Status.Code, s.Status.Description)
	}
}

func TestWithoutStatusMessage(t *testing.T) {
	orig := &stats.End{Client: true, Error: status.Error(codes.Internal, "order 42 secret")}
	got := withoutStatusMessage(orig)
	if got == orig {
		t.Fatal("a message must give a copy")
	}
	if status.Code(got.Error) != codes.Internal || status.Convert(got.Error).Message() != "" || !got.Client {
		t.Errorf("copy = %+v (%v)", got, got.Error)
	}
	if status.Convert(orig.Error).Message() != "order 42 secret" {
		t.Error("the End the other handlers get was changed")
	}
	plain := &stats.End{Error: errors.New("no code")}
	if p := withoutStatusMessage(plain); status.Code(p.Error) != codes.Unknown || status.Convert(p.Error).Message() != "" {
		t.Errorf("a plain error = %v", p.Error)
	}
	quiet := &stats.End{Error: status.Error(codes.NotFound, "")}
	if withoutStatusMessage(quiet) != quiet {
		t.Error("no message: the same End")
	}
}

// A37 review M2: the handler set a status, then failed with INTERNAL:
// otelgrpc's SetStatus replaces the application's description, with "".
func TestGRPCApplicationStatusIsReplacedOnFailure(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := errorGRPCServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := conn.Invoke(ctx, "/t.v1.S/AppStatusThenInternal", &emptypb.Empty{}, &emptypb.Empty{}); status.Code(err) != codes.Internal {
		t.Fatalf("err = %v, want INTERNAL", err)
	}
	s := spanOfKind(t, exp, trace.SpanKindServer, "t.v1.S/AppStatusThenInternal")
	if s.Status.Code != otelcodes.Error || s.Status.Description != "" {
		t.Errorf("server status = %v %q, want Error without a description", s.Status.Code, s.Status.Description)
	}
}

// A streaming (bidi) RPC takes the same End path: no description on either side.
func TestGRPCBidiStreamCarriesNoStatusMessage(t *testing.T) {
	exp := inMemoryTracing(t)
	conn := errorGRPCServer(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := conn.NewStream(ctx, &grpc.StreamDesc{ServerStreams: true, ClientStreams: true}, "/t.v1.S/Bidi")
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.SendMsg(&emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	_ = stream.CloseSend()
	if err := stream.RecvMsg(&emptypb.Empty{}); status.Code(err) != codes.Unavailable || status.Convert(err).Message() != "down" {
		t.Fatalf("recv err = %v, want UNAVAILABLE \"down\"", err)
	}
	for _, kind := range []trace.SpanKind{trace.SpanKindClient, trace.SpanKindServer} {
		s := spanOfKind(t, exp, kind, "t.v1.S/Bidi")
		if s.Status.Code != otelcodes.Error || s.Status.Description != "" {
			t.Errorf("%v status = %v %q, want Error without a description", kind, s.Status.Code, s.Status.Description)
		}
	}
}
