"""Notification Backend — gRPC server that processes notification delivery."""

import asyncio
import uuid
import random
import time
import logging

import grpc
from opentelemetry import trace

from otel_helper import setup_telemetry, get_tracer, get_meter, TelemetryOptions

import notification_pb2
import notification_pb2_grpc

# --- Telemetry ---
setup_telemetry(TelemetryOptions(
    resource_attributes={"app.component": "notification-processor"},
))

tracer = get_tracer("python-backend")
meter = get_meter("python-backend")

delivery_duration = meter.create_histogram("notification.delivery_duration_seconds")
delivery_attempts = meter.create_counter("notification.delivery_attempts_total")

logger = logging.getLogger(__name__)

# --- In-memory state ---
notifications_store: dict[str, dict] = {}

TEMPLATES = {
    "default": {"body": "Hello {name}", "subject": "Notification"},
    "welcome": {"body": "Welcome {name} to {product}", "subject": "Welcome!"},
    "alert": {"body": "Alert: {message}", "subject": "⚠️ Alert"},
}


class NotificationServicer(notification_pb2_grpc.NotificationServiceServicer):

    async def SendNotification(self, request, context):
        """Simulate sending a notification (email/sms/push)."""
        with tracer.start_as_current_span("backend.send-notification") as span:
            notification_id = str(uuid.uuid4())[:8]
            span.set_attribute("notification.id", notification_id)
            span.set_attribute("notification.channel", request.channel)
            span.set_attribute("notification.recipient", request.recipient)

            # Simulate template rendering
            with tracer.start_as_current_span("backend.render-template"):
                template = TEMPLATES.get(request.template_id, TEMPLATES["default"])
                try:
                    body = template["body"].format(**dict(request.variables))
                except KeyError:
                    body = template["body"]
                await asyncio.sleep(random.uniform(0.005, 0.02))

            # Simulate delivery (external provider call)
            with tracer.start_as_current_span("backend.deliver") as deliver_span:
                deliver_span.set_attribute("notification.provider", f"{request.channel}-provider")
                start = time.time()

                delay = {"email": 0.1, "sms": 0.05, "push": 0.02}.get(request.channel, 0.1)
                await asyncio.sleep(random.uniform(delay * 0.5, delay * 1.5))

                # Simulate occasional failures (10%)
                failed = random.random() < 0.1
                duration = time.time() - start

                delivery_duration.record(duration, {"channel": request.channel})
                delivery_attempts.add(1, {"channel": request.channel, "success": str(not failed)})

                if failed:
                    deliver_span.set_status(trace.StatusCode.ERROR, "Provider timeout")
                    status = "failed"
                else:
                    status = "sent"

            notifications_store[notification_id] = {
                "id": notification_id,
                "status": status,
                "recipient": request.recipient,
                "channel": request.channel,
                "body": body,
                "delivered_at": time.time() if status == "sent" else None,
            }

            logger.info("Notification processed", extra={
                "notification_id": notification_id,
                "status": status,
                "channel": request.channel,
            })

            return notification_pb2.SendReply(
                notification_id=notification_id,
                status=status,
            )

    async def GetStatus(self, request, context):
        """Check delivery status of a notification."""
        with tracer.start_as_current_span("backend.get-status") as span:
            span.set_attribute("notification.id", request.notification_id)

            record = notifications_store.get(request.notification_id)
            if not record:
                return notification_pb2.StatusReply(
                    notification_id=request.notification_id,
                    status="not_found",
                )

            return notification_pb2.StatusReply(
                notification_id=record["id"],
                status=record["status"],
                delivered_at=str(record["delivered_at"]) if record["delivered_at"] else "",
            )

    async def RenderTemplate(self, request, context):
        """Render a template with variables."""
        with tracer.start_as_current_span("backend.render-template") as span:
            span.set_attribute("template.id", request.template_id)

            template = TEMPLATES.get(request.template_id, TEMPLATES["default"])
            try:
                body = template["body"].format(**dict(request.variables))
            except KeyError:
                body = template["body"]

            return notification_pb2.RenderReply(
                rendered_body=body,
                subject=template["subject"],
            )


async def serve():
    server = grpc.aio.server()
    notification_pb2_grpc.add_NotificationServiceServicer_to_server(
        NotificationServicer(), server
    )

    # gRPC Health Check (standard grpc.health.v1)
    from grpc_health.v1 import health_pb2_grpc, health
    from grpc_health.v1.health_pb2 import HealthCheckResponse
    health_servicer = health.HealthServicer()
    health_servicer.set("", HealthCheckResponse.SERVING)
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, server)
    server.add_insecure_port("[::]:50051")
    logger.info("Backend gRPC server starting on :50051")
    await server.start()
    await server.wait_for_termination()


if __name__ == "__main__":
    asyncio.run(serve())
