// Package otelaws provides OpenTelemetry instrumentation for AWS SDK v2.
//
// Usage:
//
//	cfg, err := config.LoadDefaultConfig(ctx)
//	if err != nil { ... }
//	otelaws.Instrument(&cfg)
//	// All subsequent AWS SDK calls will produce trace spans.
package otelaws

import (
	"github.com/aws/aws-sdk-go-v2/aws"
	awsotel "go.opentelemetry.io/contrib/instrumentation/github.com/aws/aws-sdk-go-v2/otelaws"
)

// Instrument adds OpenTelemetry tracing middleware to an AWS SDK v2 config.
// Call this after loading the AWS config and before creating service clients.
func Instrument(cfg *aws.Config) {
	awsotel.AppendMiddlewares(&cfg.APIOptions)
}
