"""
observability.py
Initializes Phoenix (distributed tracing) and Langfuse (eval logging + prompt versioning).

Responsibilities:
  - Register the OTel tracer provider pointing at a local Phoenix instance
  - Auto-instrument Anthropic API calls via OpenInference
  - Connect to Langfuse for prompt versioning and eval score logging
  - Expose get_tracer() and get_langfuse() so research.py and run_evals.py
    can access clients without re-initializing

Call try_init_observability() once at startup in main(). The research loop and
eval runner check whether init has been called and degrade gracefully if not.

Environment variables:
  LANGFUSE_PUBLIC_KEY          Required for Langfuse
  LANGFUSE_SECRET_KEY          Required for Langfuse
  LANGFUSE_HOST                Optional; defaults to https://cloud.langfuse.com
  PHOENIX_COLLECTOR_ENDPOINT   Optional; defaults to http://localhost:6006/v1/traces
"""

import logging
import os

from langfuse import Langfuse
from openinference.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry import trace as otel_trace
from phoenix.otel import register

logger = logging.getLogger(__name__)

_tracer: otel_trace.Tracer | None = None
_langfuse: Langfuse | None = None


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------

def init_observability() -> tuple[otel_trace.Tracer, Langfuse]:
    """
    Connect to Phoenix (tracing) and Langfuse (evals/prompts).

    Phoenix: registers an OTel tracer provider exporting to a local Phoenix
    instance. AnthropicInstrumentor patches the Anthropic SDK so every
    client.messages.create() call is automatically captured as a child span
    with token counts, latency, and stop reason — no manual wrapping needed.

    Langfuse: creates a client used for prompt versioning and posting eval
    scores. Credentials come from env vars; raises KeyError if missing.

    Returns (tracer, langfuse_client) for use in tests or scripts that need
    direct access. Most callers should use get_tracer() / get_langfuse().
    """
    global _tracer, _langfuse

    # Phoenix / OpenTelemetry setup.
    # register() sets up the global OTel tracer provider and configures an
    # OTLP exporter pointing at Phoenix. After this call, any span created
    # with otel_trace.get_tracer() will appear in the Phoenix UI.
    endpoint = os.environ.get(
        "PHOENIX_COLLECTOR_ENDPOINT",
        "http://localhost:6006/v1/traces",
    )
    register(endpoint=endpoint, project_name="pm-research-assistant")

    # Auto-instrument Anthropic SDK calls. This patches anthropic.Anthropic
    # so every messages.create() becomes an OTel span — input/output tokens,
    # model, stop reason, and latency are all captured automatically.
    AnthropicInstrumentor().instrument()

    _tracer = otel_trace.get_tracer("pm-research-assistant")

    # Langfuse setup.
    _langfuse = Langfuse(
        public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
        secret_key=os.environ["LANGFUSE_SECRET_KEY"],
        host=os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com"),
    )

    logger.info(
        "Observability initialized — Phoenix at %s, Langfuse connected", endpoint
    )
    return _tracer, _langfuse


def try_init_observability() -> bool:
    """
    Initialize observability if Langfuse credentials are present in the environment.
    Silently skips (with a printed notice) if they are not set.

    Returns True if initialization succeeded, False otherwise. Either way,
    run_research() will work — it just won't emit traces or register a Langfuse
    trace for the run.
    """
    required = ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"]
    if not all(os.environ.get(k) for k in required):
        print(
            "Observability: skipped "
            "(add LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY to .env to enable)"
        )
        return False

    try:
        init_observability()
        endpoint = os.environ.get(
            "PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:6006"
        )
        print(f"Observability: Phoenix at {endpoint}, Langfuse connected")
        return True
    except Exception as exc:
        print(f"Observability: init failed ({exc}) — continuing without tracing")
        return False


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def get_tracer() -> otel_trace.Tracer:
    """
    Return the initialized OTel tracer.
    Raises RuntimeError if init_observability() has not been called.
    """
    if _tracer is None:
        raise RuntimeError(
            "Observability not initialized. Call init_observability() first."
        )
    return _tracer


def get_langfuse() -> Langfuse:
    """
    Return the initialized Langfuse client.
    Raises RuntimeError if init_observability() has not been called.
    """
    if _langfuse is None:
        raise RuntimeError(
            "Observability not initialized. Call init_observability() first."
        )
    return _langfuse


# ---------------------------------------------------------------------------
# Prompt versioning
# ---------------------------------------------------------------------------

def get_system_prompt(fallback: str) -> tuple[str, str]:
    """
    Fetch the active system prompt from Langfuse if available.

    On first use, the fallback (hardcoded) prompt is registered in Langfuse
    under the name "pm-research-system-prompt". Subsequent prompt edits made
    in the Langfuse UI create new versions, and this function always returns
    the version labeled "production".

    Returns:
        (prompt_text, version_label)
        where version_label is e.g. "pm-research-system-prompt@3" or
        "hardcoded" if Langfuse is unavailable.
    """
    try:
        lf = get_langfuse()
    except RuntimeError:
        return fallback, "hardcoded"

    prompt_name = "pm-research-system-prompt"

    try:
        # get_prompt() fetches the version currently labeled "production".
        # If no such prompt exists, it raises an exception.
        prompt_obj = lf.get_prompt(prompt_name)
        return prompt_obj.prompt, f"{prompt_name}@{prompt_obj.version}"

    except Exception:
        # Prompt doesn't exist yet — register the current hardcoded version
        # so future iterations are tracked from this baseline.
        try:
            lf.create_prompt(
                name=prompt_name,
                prompt=fallback,
                labels=["production"],
            )
            logger.info("Registered baseline prompt in Langfuse: %s", prompt_name)
        except Exception as create_exc:
            logger.warning(
                "Could not register prompt in Langfuse: %s", create_exc
            )
        return fallback, f"{prompt_name}@1"
