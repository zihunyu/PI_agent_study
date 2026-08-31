from __future__ import annotations

import asyncio
import time
import unittest

from pi_agent_loop.runtime.telemetry import (
    InMemoryTelemetryExporter,
    MetricRegistry,
    Telemetry,
)


class TelemetryFailureObservabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_export_failure_is_isolated_and_counted(self) -> None:
        def broken_sink(_payload: dict[str, object]) -> None:
            raise RuntimeError("export unavailable")

        telemetry = Telemetry(log_sink=broken_sink)

        await telemetry.log("warning", "export-test", access_token="must-not-leak")

        failures = [
            row
            for row in telemetry.metrics.snapshot()["counters"]
            if row["name"] == "telemetry_export_failures_total"
        ]
        self.assertEqual(
            failures,
            [
                {
                    "name": "telemetry_export_failures_total",
                    "labels": {
                        "channel": "log",
                        "error_type": "RuntimeError",
                    },
                    "value": 1.0,
                }
            ],
        )

    async def test_never_returning_async_sink_is_bounded_by_timeout(self) -> None:
        never = asyncio.Event()

        async def blocked_sink(_payload: dict[str, object]) -> None:
            await never.wait()

        telemetry = Telemetry(
            log_sink=blocked_sink,
            export_timeout_seconds=0.01,
        )

        await asyncio.wait_for(telemetry.log("info", "bounded"), timeout=0.5)

        counters = telemetry.metrics.snapshot()["counters"]
        self.assertTrue(
            any(
                row["name"] == "telemetry_export_timeouts_total"
                and row["value"] == 1
                for row in counters
            )
        )

    async def test_blocking_sync_sink_runs_off_event_loop_and_times_out(self) -> None:
        def blocked_sink(_payload: dict[str, object]) -> None:
            time.sleep(0.2)

        telemetry = Telemetry(
            log_sink=blocked_sink,
            export_timeout_seconds=0.01,
        )
        started = time.monotonic()

        await telemetry.log("info", "bounded-sync")

        self.assertLess(time.monotonic() - started, 0.15)

    async def test_span_event_storage_is_bounded(self) -> None:
        exporter = InMemoryTelemetryExporter()
        telemetry = Telemetry(span_sink=exporter, max_span_events=2)
        span = telemetry.start_span("bounded.events")
        for index in range(10):
            span.add_event("step", index=index)

        await span.finish()

        self.assertEqual(len(exporter.records[0]["events"]), 2)
        self.assertEqual(exporter.records[0]["droppedEvents"], 8)

    async def test_free_text_credentials_are_redacted_before_export(self) -> None:
        exporter = InMemoryTelemetryExporter()
        telemetry = Telemetry(log_sink=exporter)
        dummy_tokens = (
            "AKIA1234567890ABCDEF",
            "ghp_aaaaaaaaaaaaaaaaaaaa",
            "xoxb-1234567890-abcdefghij",
        )

        await telemetry.log(
            "warning",
            "redaction",
            message=" ".join(dummy_tokens),
        )

        rendered = repr(exporter.records)
        for token in dummy_tokens:
            self.assertNotIn(token, rendered)
        self.assertIn("[REDACTED]", rendered)


class MetricRegistryLimitTests(unittest.TestCase):
    def test_metric_series_and_label_storage_are_bounded(self) -> None:
        metrics = MetricRegistry(
            max_series_per_kind=2,
            max_labels_per_series=2,
            max_label_length=8,
        )
        for index in range(10):
            metrics.observe(
                "resource.wait",
                index,
                labels={
                    "resource": f"resource-{index}-with-long-suffix",
                    "tenant": f"tenant-{index}",
                    "extra": "ignored",
                },
            )

        snapshot = metrics.snapshot()

        self.assertEqual(len(snapshot["histograms"]), 2)
        self.assertTrue(
            all(
                len(str(value)) <= 8
                for row in snapshot["histograms"]
                for value in row["labels"].values()
            )
        )
        dropped = [
            row
            for row in snapshot["counters"]
            if row["name"] == "telemetry_metric_series_dropped_total"
        ]
        self.assertEqual(dropped[0]["value"], 8)


if __name__ == "__main__":
    unittest.main()
