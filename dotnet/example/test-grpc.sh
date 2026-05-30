#!/bin/bash
# Test gRPC endpoints for SampleBackend
# Usage:
#   ./test-grpc.sh                              # Default: plaintext localhost:5100
#   ./test-grpc.sh --plaintext localhost:5100    # Via port-forward
#   ./test-grpc.sh --plaintext <host:port>      # Any plaintext endpoint

if [[ "$1" == "-p" ]]; then
  GRPC_OPTS="-plaintext"
  ENDPOINT="${2:-localhost:5100}"
elif [[ "$1" == "-i" ]]; then
  GRPC_OPTS="-insecure"
  ENDPOINT="${2:-localhost:5100}"
else
  GRPC_OPTS="-plaintext"
  ENDPOINT="${1:-localhost:5100}"
fi

echo "=== Target: $ENDPOINT ($GRPC_OPTS) ==="
echo ""

echo ">>> 1. List services (reflection)"
grpcurl $GRPC_OPTS $ENDPOINT list

echo "TRACETRACE"
TRACE_ID=$(openssl rand -hex 16)
grpcurl $GRPC_OPTS \
  -H "traceparent: 00-${TRACE_ID}-$(openssl rand -hex 8)-01" \
  -d '{"order_id": 42, "product": "widget", "quantity": 3}' \
  $ENDPOINT order.OrderService/ProcessOrder
EXIT=$?
  echo "Trace ID: $TRACE_ID"
if [[ "$EXIT" -ne 0 ]]; then
  echo "error"
  exit 1
fi
echo ""

echo ">>> 2. Describe OrderService"
grpcurl $GRPC_OPTS $ENDPOINT describe order.OrderService
echo ""

echo ">>> 3. ProcessOrder"
grpcurl $GRPC_OPTS -d '{"order_id": 42, "product": "widget", "quantity": 3}' \
  $ENDPOINT order.OrderService/ProcessOrder
echo ""

echo ">>> 4. CancelOrder"
grpcurl $GRPC_OPTS -d '{"order_id": 42}' \
  $ENDPOINT order.OrderService/CancelOrder
echo ""

echo ">>> 5. SlowOperation (4-7s)"
grpcurl $GRPC_OPTS $ENDPOINT order.OrderService/SlowOperation
echo ""

echo ">>> 6. UnstableOperation"
grpcurl $GRPC_OPTS -d '{"request_id": 1, "fail_count": 0}' \
  $ENDPOINT order.OrderService/UnstableOperation
echo ""

echo ">>> 7. ReadBaggage"
grpcurl $GRPC_OPTS $ENDPOINT order.OrderService/ReadBaggage
echo ""

echo "=== Done ==="
