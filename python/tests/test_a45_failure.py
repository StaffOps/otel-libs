"""Application span failure rule (task A45: R11, ADR-016, contract design D15).

In-process cases use an in-memory span exporter and a handler on the
``staffops_otel_helper.failure`` logger that also records the span context
current at emit time. Routing cases (stdout fallback, dictConfig, OTLP
export, before setup) run in a fresh interpreter, as in test_logging_a5.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import subprocess
import sys
import textwrap
import threading

import grpc
import pytest

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF, ParentBased
from opentelemetry.trace import Status, StatusCode

import staffops_otel_helper
from staffops_otel_helper import _console, _failure, record_failure, start_root_span, traced

FAILURE_LOGGER = "staffops_otel_helper.failure"


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.NOTSET)
        self.records: list[tuple[logging.LogRecord, trace.SpanContext]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record, trace.get_current_span().get_span_context()))


@pytest.fixture
def spans():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("a45"), exporter
    provider.shutdown()


@pytest.fixture
def records():
    capture = _Capture()
    lg = logging.getLogger(FAILURE_LOGGER)
    level = lg.level
    lg.addHandler(capture)
    yield capture
    lg.removeHandler(capture)
    lg.setLevel(level)


def _span(exporter, name):
    return next(s for s in exporter.get_finished_spans() if s.name == name)


def _assert_marked(span, error_type):
    assert span.status.status_code is StatusCode.ERROR
    assert not span.status.description
    assert span.attributes["error.type"] == error_type
    assert list(span.events) == []


def _assert_record(entry, span, exc):
    record, ctx = entry
    assert record.levelno == logging.ERROR
    assert record.getMessage() == str(exc)
    assert record.exc_info is None
    assert getattr(record, "exception.type") == span.attributes["error.type"]
    assert getattr(record, "exception.message") == str(exc)
    assert type(exc).__name__ in getattr(record, "exception.stacktrace")
    assert ctx.trace_id == span.context.trace_id and ctx.span_id == span.context.span_id


class OrderError(Exception):
    pass


class Overridden(Exception):
    __otel_error_type__ = "OrderRejectedError"


@dataclasses.dataclass
class DataError(Exception):
    code: int


class TestFailure:
    def test_context_manager_marks_and_propagates_by_identity(self, spans, records):
        tracer, exporter = spans
        exc = OrderError("boom")
        with pytest.raises(OrderError) as info:
            with traced("work", tracer=tracer):
                raise exc
        assert info.value is exc
        span = _span(exporter, "work")
        _assert_marked(span, f"{__name__}.OrderError")
        assert len(records.records) == 1
        _assert_record(records.records[0], span, exc)

    def test_success_stays_unset_without_record(self, spans, records):
        tracer, exporter = spans
        with traced("ok", tracer=tracer) as span:
            assert span.is_recording()

        @traced("ok-fn", tracer=tracer)
        def fn(x):
            return x * 2

        assert fn(2) == 4
        for name in ("ok", "ok-fn"):
            assert _span(exporter, name).status.status_code is StatusCode.UNSET
        assert records.records == []

    def test_sync_decorator(self, spans, records):
        tracer, exporter = spans

        @traced("fn", tracer=tracer, attributes={"a": 1})
        def fn():
            raise ValueError("bad value")

        assert fn.__name__ == "fn"
        with pytest.raises(ValueError):
            fn()
        span = _span(exporter, "fn")
        _assert_marked(span, "ValueError")
        assert span.attributes["a"] == 1
        assert len(records.records) == 1

    def test_async_decorator(self, spans, records):
        tracer, exporter = spans

        @traced("afn", tracer=tracer)
        async def afn(fail):
            await asyncio.sleep(0)
            if fail:
                raise OrderError("async boom")
            return "done"

        assert asyncio.run(afn(False)) == "done"
        with pytest.raises(OrderError) as info:
            asyncio.run(afn(True))
        finished = [s for s in exporter.get_finished_spans() if s.name == "afn"]
        assert [s.status.status_code for s in finished] == [StatusCode.UNSET, StatusCode.ERROR]
        assert len(records.records) == 1
        _assert_record(records.records[0], finished[1], info.value)

    def test_override_wins_and_plain_field_ignored(self, spans, records):
        tracer, exporter = spans
        with pytest.raises(Overridden):
            with traced("cls", tracer=tracer):
                raise Overridden("x")
        inst = OrderError("y")
        inst.__otel_error_type__ = "InstanceType"
        with pytest.raises(OrderError):
            with traced("inst", tracer=tracer):
                raise inst
        plain = OrderError("z")
        plain.error_type = "LibraryField"
        with pytest.raises(OrderError):
            with traced("plain", tracer=tracer):
                raise plain
        empty = OrderError("w")
        empty.__otel_error_type__ = ""
        with pytest.raises(OrderError):
            with traced("empty", tracer=tracer):
                raise empty
        assert _span(exporter, "cls").attributes["error.type"] == "OrderRejectedError"
        assert getattr(records.records[0][0], "exception.type") == "OrderRejectedError"
        assert _span(exporter, "inst").attributes["error.type"] == "InstanceType"
        assert _span(exporter, "plain").attributes["error.type"] == f"{__name__}.OrderError"
        assert _span(exporter, "empty").attributes["error.type"] == f"{__name__}.OrderError"

    def test_hostile_override_lookup_and_str(self, spans, records):
        tracer, exporter = spans

        class Hostile(Exception):
            def __getattr__(self, name):
                if name == "__otel_error_type__":
                    raise RuntimeError("no")
                raise AttributeError(name)

            def __str__(self):
                raise RuntimeError("no str")

        with pytest.raises(Hostile):
            with traced("hostile", tracer=tracer):
                raise Hostile()
        assert _span(exporter, "hostile").status.status_code is StatusCode.ERROR
        assert records.records[0][0].getMessage() == "[unprintable Hostile]"

    def test_internal_error_is_warned_never_raised_without_the_app_exception(self, caplog):
        class Broken(trace.NonRecordingSpan):
            def set_status(self, *a, **k):
                raise RuntimeError("broken span")

        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
            try:
                raise OrderError("app-secret-detail")
            except OrderError as e:  # the internal error gets this exception as __context__
                record_failure(e, span=Broken(trace.INVALID_SPAN_CONTEXT))
        (warning,) = [r for r in caplog.records if r.name == "staffops_otel_helper.tracing"]
        assert "Could not record a failure of type OrderError on its span (RuntimeError)" in warning.getMessage()
        assert warning.exc_info is None
        assert "app-secret-detail" not in caplog.text


class TestCancellation:
    @pytest.mark.parametrize("exc", [asyncio.CancelledError(), KeyboardInterrupt(), SystemExit(3),
                                     GeneratorExit()])
    def test_base_exceptions_stay_unset(self, spans, records, exc):
        tracer, exporter = spans
        with pytest.raises(type(exc)) as info:
            with traced("cancel", tracer=tracer):
                raise exc
        assert info.value is exc
        assert _span(exporter, "cancel").status.status_code is StatusCode.UNSET
        assert "error.type" not in _span(exporter, "cancel").attributes
        assert records.records == []

    def test_async_cancellation(self, spans, records):
        tracer, exporter = spans

        @traced("acancel", tracer=tracer)
        async def wait():
            await asyncio.sleep(10)

        async def main():
            task = asyncio.ensure_future(wait())
            await asyncio.sleep(0)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(main())
        assert _span(exporter, "acancel").status.status_code is StatusCode.UNSET
        assert records.records == []

    @pytest.mark.parametrize("exc", [TimeoutError("t"), asyncio.TimeoutError()])
    def test_timeouts_are_failures(self, spans, records, exc):
        tracer, exporter = spans
        with pytest.raises(type(exc)):
            with traced("timeout", tracer=tracer):
                raise exc
        assert _span(exporter, "timeout").status.status_code is StatusCode.ERROR
        assert len(records.records) == 1

    class _Rpc(grpc.RpcError):
        def __init__(self, code):
            self._code = code

        def code(self):
            if isinstance(self._code, BaseException):
                raise self._code
            return self._code

    def test_grpc_cancelled_stays_unset(self, spans, records):
        tracer, exporter = spans
        with pytest.raises(grpc.RpcError):
            with traced("grpc-cancel", tracer=tracer):
                raise self._Rpc(grpc.StatusCode.CANCELLED)
        assert _span(exporter, "grpc-cancel").status.status_code is StatusCode.UNSET
        assert records.records == []

    @pytest.mark.parametrize("exc", [
        _Rpc(grpc.StatusCode.DEADLINE_EXCEEDED), _Rpc(RuntimeError("code() fails")), grpc.RpcError(),
    ])
    def test_other_grpc_errors_are_failures(self, spans, records, exc):
        tracer, exporter = spans
        with pytest.raises(grpc.RpcError):
            with traced("grpc-fail", tracer=tracer):
                raise exc
        assert _span(exporter, "grpc-fail").status.status_code is StatusCode.ERROR
        assert len(records.records) == 1

    def test_grpc_not_imported_is_never_imported(self, monkeypatch):
        exc = self._Rpc(grpc.StatusCode.CANCELLED)
        monkeypatch.delitem(sys.modules, "grpc")
        assert _failure.is_cancellation(exc) is False
        assert "grpc" not in sys.modules

    def test_wrapped_cancellation_is_a_failure(self, spans, records):
        tracer, exporter = spans
        with pytest.raises(OrderError):
            with traced("wrapped-cancel", tracer=tracer):
                raise OrderError("x") from asyncio.CancelledError()
        assert _span(exporter, "wrapped-cancel").status.status_code is StatusCode.ERROR

    @staticmethod
    def _groups():
        try:
            return BaseExceptionGroup, ExceptionGroup  # noqa: F821 - builtins from 3.11
        except NameError:
            backport = pytest.importorskip("exceptiongroup")
            return backport.BaseExceptionGroup, backport.ExceptionGroup

    def test_group_of_cancellations_is_a_cancellation(self, spans, records):
        BEG, EG = self._groups()
        tracer, exporter = spans
        nested = BEG("outer", [asyncio.CancelledError(), BEG("inner", [KeyboardInterrupt()])])
        with pytest.raises(BEG):
            with traced("group-cancel", tracer=tracer):
                raise nested
        assert _span(exporter, "group-cancel").status.status_code is StatusCode.UNSET
        assert records.records == []
        assert _failure.is_cancellation(EG("rpc", [self._Rpc(grpc.StatusCode.CANCELLED)])) is True

    def test_group_with_a_failure_leaf_is_a_failure(self, spans, records):
        BEG, EG = self._groups()
        tracer, exporter = spans
        mixed = BEG("mixed", [asyncio.CancelledError(), BEG("inner", [ValueError("leaf")])])
        with pytest.raises(BEG):
            with traced("group-fail", tracer=tracer):
                raise mixed
        assert _span(exporter, "group-fail").status.status_code is StatusCode.ERROR
        assert len(records.records) == 1
        assert _failure.is_cancellation(EG("plain", [ValueError("v")])) is False

    def test_group_walk_is_bounded(self):
        BEG, _ = self._groups()
        group = BEG("leaf", [asyncio.CancelledError()])
        for i in range(300):
            group = BEG(f"level-{i}", [group])
        assert _failure.is_cancellation(group) is False  # beyond 256 nodes: a failure
        assert _failure.is_cancellation(BEG("small", [asyncio.CancelledError()] * 3)) is True  # the same object 3 times

    def test_hostile_group_is_a_failure(self):
        BEG, _ = self._groups()

        class Hostile(BEG):
            @property
            def exceptions(self):
                raise RuntimeError("no members")

        assert _failure.is_cancellation(Hostile("h", [asyncio.CancelledError()])) is False


class TestNesting:
    """design D15 "Nesting" (owner ruling 2026-11-17): one record per helper span that marks the failure."""

    @pytest.mark.parametrize("make", [lambda: ValueError("v"), lambda: DataError(7)])
    def test_three_spans_three_records(self, spans, records, make):
        tracer, exporter = spans
        exc = make()
        with pytest.raises(type(exc)) as info:
            with traced("a", tracer=tracer):
                with traced("b", tracer=tracer):
                    with traced("c", tracer=tracer):
                        raise exc
        assert info.value is exc
        a, b, c = (_span(exporter, n) for n in "abc")
        for span in (a, b, c):
            _assert_marked(span, error_type := c.attributes["error.type"])
        assert error_type == ("ValueError" if isinstance(exc, ValueError) else f"{__name__}.DataError")
        assert [ctx.span_id for _, ctx in records.records] == [c.context.span_id, b.context.span_id,
                                                               a.context.span_id]
        for entry, span in zip(records.records, (c, b, a)):
            _assert_record(entry, span, exc)

    def test_wrapped_layers_record_each_layer(self, spans, records):
        tracer, exporter = spans
        with pytest.raises(RuntimeError):
            with traced("l1", tracer=tracer):
                try:
                    with traced("l2", tracer=tracer):
                        raise ValueError("root cause")
                except ValueError as e:
                    raise RuntimeError("top") from e
        assert [r.getMessage() for r, _ in records.records] == ["root cause", "top"]
        assert _span(exporter, "l1").attributes["error.type"] == "RuntimeError"

    def test_record_failure_always_records(self, spans, records):
        tracer, _ = spans
        exc = OrderError("handled")
        with pytest.raises(OrderError):
            with traced("outer", tracer=tracer):
                record_failure(exc)
                record_failure(exc)
                raise exc
        assert len(records.records) == 3

    def test_no_object_mark(self, spans):
        tracer, _ = spans
        exc = ValueError("x")
        with pytest.raises(ValueError):
            with traced("t", tracer=tracer):
                raise exc
        assert not any(k.startswith("_staffops") for k in vars(exc))


class TestRecordFailure:
    def test_current_span_by_default(self, spans, records):
        tracer, exporter = spans
        with traced("handled", tracer=tracer):
            record_failure(OrderError("rejected"))
        span = _span(exporter, "handled")
        _assert_marked(span, f"{__name__}.OrderError")
        assert len(records.records) == 1

    def test_explicit_span_not_current_gets_its_ids(self, spans, records):
        tracer, exporter = spans
        held = tracer.start_span("held")
        with tracer.start_as_current_span("current"):
            record_failure(OrderError("late"), span=held)
        held.end()
        assert records.records[0][1].span_id == _span(exporter, "held").context.span_id
        assert _span(exporter, "current").status.status_code is StatusCode.UNSET

    def test_unsampled_span_still_emits_with_ids(self, records):
        provider = TracerProvider(sampler=ParentBased(ALWAYS_OFF))
        tracer = provider.get_tracer("a45")
        with pytest.raises(OrderError):
            with traced("unsampled", tracer=tracer) as span:
                assert not span.is_recording()
                raise OrderError("x")
        assert len(records.records) == 1
        assert records.records[0][1].span_id == span.get_span_context().span_id

    def test_without_span_still_emits(self, records):
        exc = OrderError("no span")
        record_failure(exc)
        record_failure(exc)
        assert len(records.records) == 2
        assert not records.records[0][1].is_valid

    def test_ok_is_final_but_type_and_record_added(self, spans, records):
        tracer, exporter = spans
        with traced("ok-first", tracer=tracer) as span:
            span.set_status(Status(StatusCode.OK))
            record_failure(OrderError("x"))
        span = _span(exporter, "ok-first")
        assert span.status.status_code is StatusCode.OK
        assert span.attributes["error.type"] == f"{__name__}.OrderError"
        assert len(records.records) == 1

    def test_invalid_input_fails_fast(self):
        with pytest.raises(TypeError):
            record_failure("not an exception")
        with pytest.raises(TypeError):
            record_failure(OrderError("x"), span="span")

    def test_marking_without_log_pipeline(self, spans, records):
        tracer, exporter = spans
        logging.getLogger(FAILURE_LOGGER).setLevel(logging.CRITICAL)  # the app silenced the failure logger itself
        with pytest.raises(OrderError):
            with traced("silent", tracer=tracer):
                raise OrderError("x")
        assert records.records == []
        _assert_marked(_span(exporter, "silent"), f"{__name__}.OrderError")

    def test_diagnostics_level_does_not_drop_records(self, spans, records):
        tracer, _ = spans
        logging.getLogger(FAILURE_LOGGER).setLevel(logging.NOTSET)
        logging.getLogger("staffops_otel_helper").setLevel(logging.CRITICAL)  # quiets the helper's diagnostics
        with pytest.raises(OrderError):
            with traced("loud", tracer=tracer):
                raise OrderError("x")
        assert len(records.records) == 1
        assert logging.getLogger(FAILURE_LOGGER).level == logging.DEBUG  # set explicitly at emit time

    def test_propagate_false_handler_on_helper_logger_still_captures(self, spans):
        tracer, _ = spans
        helper = logging.getLogger("staffops_otel_helper")
        capture = _Capture()
        helper.addHandler(capture)
        helper.propagate = False
        try:
            with pytest.raises(OrderError):
                with traced("captured", tracer=tracer):
                    raise OrderError("x")
        finally:
            helper.removeHandler(capture)
            helper.propagate = True
        assert [r.name for r, _ in capture.records] == [FAILURE_LOGGER]


class TestSharedInstance:
    """One ``traced(...)`` object entered by concurrent tasks or threads exits each one's own scope."""

    @staticmethod
    def _check(exporter, records, caplog):
        spans = [s for s in exporter.get_finished_spans() if s.name == "shared"]
        by_type = {s.attributes["error.type"]: s for s in spans}
        assert set(by_type) == {"ValueError", "KeyError"}
        assert all(s.parent is None for s in spans)  # neither is the other's child
        for record, ctx in records.records:
            assert ctx.span_id == by_type[getattr(record, "exception.type")].context.span_id
        assert len(records.records) == 2
        assert "Failed to detach context" not in caplog.text

    def test_exit_without_enter_in_this_context_is_a_clear_error(self, spans):
        tracer, _ = spans
        block = traced("shared", tracer=tracer)
        with pytest.raises(RuntimeError, match="without a matching __enter__"):
            block.__exit__(None, None, None)

    def test_two_tasks_exit_in_reverse_order(self, spans, records, caplog):
        tracer, exporter = spans
        block = traced("shared", tracer=tracer)

        async def main():
            a_in, b_in, a_out = asyncio.Event(), asyncio.Event(), asyncio.Event()

            async def first():
                with block:
                    a_in.set()
                    await b_in.wait()
                    a_out.set()
                    raise ValueError("first exits first")

            async def second():
                await a_in.wait()
                with block:
                    b_in.set()
                    await a_out.wait()
                    await asyncio.sleep(0)
                    raise KeyError("second exits last")

            return await asyncio.gather(first(), second(), return_exceptions=True)

        with caplog.at_level(logging.ERROR):
            results = asyncio.run(main())
        assert [type(r) for r in results] == [ValueError, KeyError]
        self._check(exporter, records, caplog)

    def test_two_threads_share_one_instance(self, spans, records, caplog):
        tracer, exporter = spans
        block = traced("shared", tracer=tracer)
        both_in, first_out = threading.Barrier(2), threading.Event()
        errors = []

        def run(exc, wait_first):
            try:
                with block:
                    both_in.wait()
                    if wait_first:
                        first_out.wait(5)
                    raise exc
            except Exception as e:  # noqa: BLE001
                errors.append(type(e))
            finally:
                if not wait_first:
                    first_out.set()

        threads = [threading.Thread(target=run, args=(ValueError("t1"), False)),
                   threading.Thread(target=run, args=(KeyError("t2"), True))]
        with caplog.at_level(logging.ERROR):
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        assert sorted(e.__name__ for e in errors) == ["KeyError", "ValueError"]
        self._check(exporter, records, caplog)


class TestApi:
    def test_name_must_be_a_str(self):
        with pytest.raises(TypeError, match="name must be a str"):
            traced(42)

    def test_exported(self):
        assert {"traced", "record_failure"} <= set(staffops_otel_helper.__all__)

    @pytest.mark.parametrize("kw", ["record_exception", "set_status_on_exception"])
    def test_forbidden_kwargs(self, spans, kw):
        tracer, _ = spans
        with pytest.raises(TypeError, match=kw):
            traced("x", tracer=tracer, **{kw: True})
        with pytest.raises(TypeError, match=kw):
            with start_root_span(tracer, "x", **{kw: False}):
                pass

    def test_generator_functions_rejected(self):
        def gen():
            yield 1

        async def agen():
            yield 1

        for fn in (gen, agen):
            with pytest.raises(TypeError, match="generator"):
                traced("x")(fn)

    def test_awaitable_result_warns_once(self, spans, caplog):
        tracer, _ = spans

        class AsyncCallable:
            async def __call__(self):
                return 1

        wrapped = traced("w", tracer=tracer)(AsyncCallable())
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
            coros = [wrapped(), wrapped()]
        for c in coros:
            assert asyncio.run(c) == 1
        assert caplog.text.count("the span ended when the call returned") == 1

    def test_generator_result_warns(self, spans, caplog):
        tracer, _ = spans
        wrapped = traced("g", tracer=tracer)(lambda: (i for i in range(2)))
        with caplog.at_level(logging.WARNING, logger="staffops_otel_helper.tracing"):
            assert list(wrapped()) == [0, 1]
        assert "returned generator" in caplog.text

    def test_default_tracer_and_reused_context_manager(self, spans):
        tracer, exporter = spans
        trace.set_tracer_provider(TracerProvider())
        provider = trace.get_tracer_provider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        block = traced("reused")
        with block as first:
            with block as second:
                assert second.parent.span_id == first.get_span_context().span_id
        assert len([s for s in exporter.get_finished_spans() if s.name == "reused"]) == 2

    def test_root_honors_explicit_context(self, spans):
        tracer, exporter = spans
        with tracer.start_as_current_span("parent") as parent:
            ctx = trace.set_span_in_context(parent)
        with traced("child", tracer=tracer, root=True, context=ctx):
            pass
        assert _span(exporter, "child").parent.span_id == parent.get_span_context().span_id


class TestStartRootSpan:
    def test_follows_the_rule(self, spans, records):
        tracer, exporter = spans
        with pytest.raises(OrderError):
            with tracer.start_as_current_span("parent"):
                with start_root_span(tracer, "root", attributes={"batch.size": 2}):
                    raise OrderError("secret detail")
        span = _span(exporter, "root")
        assert span.parent is None
        _assert_marked(span, f"{__name__}.OrderError")
        assert span.attributes["batch.size"] == 2
        assert "secret detail" not in repr(span.to_json())
        assert len(records.records) == 1 and records.records[0][1].span_id == span.context.span_id


class TestRouting:
    """Unit level: the failure logger is routed as an application record (D6 note 2026-11-17)."""

    def _record(self, name=FAILURE_LOGGER):
        return logging.LogRecord(name, logging.ERROR, __file__, 1, "FAIL-REC", None, None)

    def test_predicates(self):
        assert _console.is_failure_record(FAILURE_LOGGER)
        assert _console.is_failure_record(FAILURE_LOGGER + ".child")
        assert not _console.is_failure_record("staffops_otel_helper.failures")
        assert _console.is_diagnostic(FAILURE_LOGGER)  # still re-enabled after dictConfig

    def test_console_fallback_sends_it_to_stdout(self, capsys):
        fallback = _console.ConsoleFallback(logging.INFO, logging.lastResort)
        fallback.handle(self._record())
        out = capsys.readouterr()
        assert "FAIL-REC" in out.out and "FAIL-REC" not in out.err

    def test_stderr_handlers_ignore_it(self, capsys):
        assert _console.DiagnosticsToStderr(logging.lastResort, ()).handle(self._record()) is False
        setup = _console.SetupDiagnostics(logging.lastResort)
        assert setup.handle(self._record()) is True  # what the stdlib lastResort would do (plain stderr)
        assert "FAIL-REC" in capsys.readouterr().err

    def test_stdout_ids_and_attribute_collision(self):
        fmt = _console.JSONFormatter()
        record = self._record("app")
        record.trace_id = "attr"
        assert json.loads(fmt.format(record))["trace_id"] == "attr"  # no span context: the attribute stays
        span = trace.NonRecordingSpan(trace.SpanContext(0xABC, 0xDEF, is_remote=False))
        with trace.use_span(span):
            line = fmt.format(record)
        assert '"logger":"app","trace_id":"00000000000000000000000000000abc","span_id":"0000000000000def"' in line
        assert line.count("trace_id") == 1


# ── fresh interpreter: real routing ───────────────────────────────────────────

_PRELUDE = """
import os
if os.environ.get("PARITY_COVERAGE_DATA"):
    import coverage
    _cov = coverage.Coverage(data_file=os.environ["PARITY_COVERAGE_DATA"], data_suffix=True,
                             source=["staffops_otel_helper"], config_file=False)
    _cov.start()
    import atexit
    atexit.register(lambda: (_cov.stop(), _cov.save()))
import json, logging, logging.config, sys
from staffops_otel_helper import setup_telemetry, shutdown_telemetry, traced
def fail(name="work"):
    try:
        with traced(name) as span:
            raise ValueError("FAIL-DETAIL")
    except ValueError:
        return span.get_span_context()
"""


def _run(body: str, **env: str) -> subprocess.CompletedProcess:
    full_env = {k: v for k, v in os.environ.items()
                if not (k.startswith("OTEL_") or k in ("SERVICE_NAME", "ENVIRONMENT"))}
    full_env.update({"OTEL_SERVICE_NAME": "a45", "OTEL_HELPER_METRICS_PORT": "0",
                     "OTEL_TRACES_EXPORTER": "none", "OTEL_METRICS_EXPORTER": "none", "ENVIRONMENT": "PRD"})
    full_env.update(env)
    if os.environ.get("PARITY_COVERAGE_DATA"):
        full_env["PARITY_COVERAGE_DATA"] = os.environ["PARITY_COVERAGE_DATA"]
    proc = subprocess.run([sys.executable, "-c", _PRELUDE + textwrap.dedent(body)], env=full_env,
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return proc


def _stdout_records(proc):
    return [json.loads(line) for line in proc.stdout.splitlines() if "FAIL-DETAIL" in line]


class TestRoutingEndToEnd:
    def test_stdout_fallback_carries_ids_and_attributes(self):
        proc = _run("""
            setup_telemetry()
            ctx = fail()
            print("IDS", format(ctx.trace_id, "032x"), format(ctx.span_id, "016x"), flush=True)
        """)
        (line,) = _stdout_records(proc)
        ids = next(x for x in proc.stdout.splitlines() if x.startswith("IDS")).split()
        assert line["level"] == "ERROR" and line["msg"] == "FAIL-DETAIL"
        assert line["logger"] == "staffops_otel_helper.failure"
        assert (line["trace_id"], line["span_id"]) == (ids[1], ids[2])
        assert line["exception.type"] == "ValueError" and line["exception.message"] == "FAIL-DETAIL"
        assert "Traceback" in line["exception.stacktrace"] and "exception" not in line
        assert "FAIL-DETAIL" not in proc.stderr

    def test_dict_config_without_exporter_reenabled_at_emit(self):
        proc = _run("""
            logging.getLogger("staffops_otel_helper.failure")  # exists before dictConfig: disabled by it
            setup_telemetry()
            logging.config.dictConfig({"version": 1})
            fail()
        """)
        assert len(_stdout_records(proc)) == 1

    def test_dict_config_with_app_handler_reaches_it(self):
        proc = _run("""
            setup_telemetry()
            logging.config.dictConfig({"version": 1, "handlers": {"h": {
                "class": "logging.StreamHandler", "stream": "ext://sys.stdout", "formatter": "f"}},
                "formatters": {"f": {"format": "APP %(name)s %(message)s"}}, "root": {"handlers": ["h"]}})
            fail()
        """)
        assert "APP staffops_otel_helper.failure FAIL-DETAIL" in proc.stdout
        assert "FAIL-DETAIL" not in proc.stderr

    def test_dict_config_with_exporter_still_exported_never_stderr(self):
        proc = _run("""
            setup_telemetry()
            logging.config.dictConfig({"version": 1})
            fail()
            shutdown_telemetry()
        """, OTEL_LOGS_EXPORTER="console")
        exported = [line for line in proc.stdout.splitlines() if '"body": "FAIL-DETAIL"' in line]
        assert len(exported) == 1, proc.stdout
        assert '"exception.type": "ValueError"' in proc.stdout
        assert "FAIL-DETAIL" not in proc.stderr

    def test_before_setup_goes_to_stderr_plain_text(self):
        proc = _run("""
            fail()
        """)
        assert "FAIL-DETAIL" in proc.stderr and "FAIL-DETAIL" not in proc.stdout
