"""Notification API — FastAPI service that receives notification requests."""

import os
import time
import random
import logging
from contextlib import asynccontextmanager

import grpc
from fastapi import FastAPI
from pydantic import BaseModel
from opentelemetry import trace

from otel_helper import setup_telemetry, get_tracer, get_meter, TelemetryOptions

# --- Telemetry ---
setup_telemetry(TelemetryOptions(
    resource_attributes={"app.component": "notification-api"},
))

tracer = get_tracer("python-api")
meter = get_meter("python-api")

notifications_sent = meter.create_counter("notifications.sent_total")
notifications_failed = meter.create_counter("notifications.failed_total")
template_renders = meter.create_counter("templates.rendered_total")
active_requests = meter.create_up_down_counter("notifications.active_requests")

logger = logging.getLogger(__name__)

# --- gRPC client (lazy import of generated stubs) ---
import notification_pb2
import notification_pb2_grpc

BACKEND_URL = os.getenv("BACKEND_URL", "python-backend-active:50051")

# --- Models ---
class NotifyRequest(BaseModel):
    recipient: str
    channel: str = "email"
    template_id: str = "default"
    variables: dict[str, str] = {}

class TemplateRequest(BaseModel):
    name: str
    body: str
    subject: str

# --- In-memory store ---
templates_db: dict[str, dict] = {
    "default": {"name": "default", "body": "Hello {{name}}", "subject": "Notification"},
    "welcome": {"name": "welcome", "body": "Welcome {{name}} to {{product}}", "subject": "Welcome!"},
    "alert": {"name": "alert", "body": "Alert: {{message}}", "subject": "⚠️ Alert"},
}
notifications_db: dict[str, dict] = {}

# --- App ---
app = FastAPI(title="Notification API")

# Instrument the app for automatic context extraction from incoming requests
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
FastAPIInstrumentor.instrument_app(app, excluded_urls="/health,/healthz,/ready,/ping")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/notify")
async def send_notification(req: NotifyRequest):
    """Send a notification via the backend gRPC service."""
    active_requests.add(1)
    try:
        with tracer.start_as_current_span("api.send-notification") as span:
            span.set_attribute("notification.channel", req.channel)
            span.set_attribute("notification.recipient", req.recipient)
            span.set_attribute("notification.template", req.template_id)

            async with grpc.aio.insecure_channel(BACKEND_URL) as channel:
                stub = notification_pb2_grpc.NotificationServiceStub(channel)

                response = await stub.SendNotification(
                    notification_pb2.SendRequest(
                        recipient=req.recipient,
                        channel=req.channel,
                        template_id=req.template_id,
                        variables=req.variables,
                    )
                )

            notifications_db[response.notification_id] = {
                "id": response.notification_id,
                "status": response.status,
                "recipient": req.recipient,
                "channel": req.channel,
                "created_at": time.time(),
            }

            if response.status == "failed":
                notifications_failed.add(1, {"channel": req.channel})
                span.set_status(trace.StatusCode.ERROR, "Backend reported failure")
            else:
                notifications_sent.add(1, {"channel": req.channel})

            logger.info("Notification sent", extra={
                "notification_id": response.notification_id,
                "status": response.status,
            })

            return {"notification_id": response.notification_id, "status": response.status}
    finally:
        active_requests.add(-1)


@app.get("/notify/{notification_id}")
async def get_notification_status(notification_id: str):
    """Check notification delivery status."""
    with tracer.start_as_current_span("api.get-status") as span:
        span.set_attribute("notification.id", notification_id)

        if notification_id in notifications_db:
            return notifications_db[notification_id]

        # Ask backend
        async with grpc.aio.insecure_channel(BACKEND_URL) as channel:
            stub = notification_pb2_grpc.NotificationServiceStub(channel)
            response = await stub.GetStatus(
                notification_pb2.StatusRequest(notification_id=notification_id)
            )
            return {
                "id": response.notification_id,
                "status": response.status,
                "delivered_at": response.delivered_at or None,
            }


@app.get("/templates")
async def list_templates():
    """List available notification templates."""
    return list(templates_db.values())


@app.post("/templates")
async def create_template(req: TemplateRequest):
    """Create a new notification template."""
    with tracer.start_as_current_span("api.create-template") as span:
        span.set_attribute("template.name", req.name)
        templates_db[req.name] = {"name": req.name, "body": req.body, "subject": req.subject}
        template_renders.add(1)
        return {"status": "created", "name": req.name}


@app.get("/metrics/summary")
async def metrics_summary():
    """Business metrics summary."""
    return {
        "total_notifications": len(notifications_db),
        "templates_available": len(templates_db),
        "channels": list(set(n["channel"] for n in notifications_db.values())),
    }
