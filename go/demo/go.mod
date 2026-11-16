module github.com/staffops/otel-libs/go/demo

go 1.26.0

require (
	google.golang.org/grpc v1.83.2
	google.golang.org/protobuf v1.36.12
)

require (
	golang.org/x/net v0.58.0 // indirect
	golang.org/x/sys v0.47.0 // indirect
	golang.org/x/text v0.41.0 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260526163538-3dc84a4a5aaa // indirect
)

// The demo apps (task DE3a) use the local helper modules, as the examples do
// (go/example/go-api/go.mod:57). The core module gains no dependency.
replace (
	github.com/staffops/otel-libs/go => ../
	github.com/staffops/otel-libs/go/ext/otelredis => ../ext/otelredis
	github.com/staffops/otel-libs/go/ext/otelsql => ../ext/otelsql
)
