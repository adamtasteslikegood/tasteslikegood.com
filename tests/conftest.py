"""Shared pytest setup.

Disable the Datadog tracer for the test run. Application code opens manual
spans (KAN-268); with tracing on, the writer tries to flush them to a local
agent that does not exist and prints "failed to send, dropping N traces" at
exit. ``tracer.trace()`` still returns a usable span when disabled.

Setting ``DD_TRACE_ENABLED`` here would be too late: the ddtrace pytest plugin
imports ddtrace, and reads its config, before any conftest runs.
"""

from ddtrace.trace import tracer

tracer.enabled = False
