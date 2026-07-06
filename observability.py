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

def _resolve_versioned_prompt(prompt_name: str, fallback: str) -> tuple[str, str]:
    """
    Fetch a named prompt from Langfuse, registering it from the fallback if
    it doesn't exist yet. Shared by get_system_prompt and get_eval_prompt so
    the not-found handling lives in exactly one place.

    Returns:
        (prompt_text, version_label)

        version_label is one of:
          "<name>@3"                     normal case, the fetched version
          "hardcoded"                    Langfuse not initialized at all
          "hardcoded-lookup-failed"      lookup errored (auth, hard network)
          "hardcoded-register-failed"    prompt was missing and create failed

        The distinct failure labels matter: they get written into artifacts,
        so a run that fell back to the hardcoded prompt is never silently
        recorded as if it ran a real registered version.
    """
    try:
        lf = get_langfuse()
    except RuntimeError:
        return fallback, "hardcoded"

    # We need to tell two failures apart:
    #   1. The prompt genuinely doesn't exist yet. This raises
    #      LangfuseNotFoundError (a 404). Registering a baseline is the
    #      right move here.
    #   2. Anything else — auth failure, or a hard network failure with
    #      no cached copy to fall back on. Registering a prompt here is
    #      wrong. It can create a junk version that maps to no real prompt
    #      edit, and it means returning a made-up version label that later
    #      gets written into an artifact as fact.
    #
    # LangfuseNotFoundError has moved around across SDK versions, so we
    # resolve it by name at runtime rather than importing it at module top
    # (a wrong import path there would break this whole module on load). If
    # we can't find it, NotFoundDummy never matches, so every error falls
    # through to the safe branch.
    try:
        from langfuse import errors as _lf_errors
        NotFoundError = getattr(_lf_errors, "LangfuseNotFoundError", None)
    except Exception:
        NotFoundError = None

    if NotFoundError is None:
        class NotFoundDummy(Exception):
            pass
        NotFoundError = NotFoundDummy

    try:
        # get_prompt() returns the version currently labeled "production".
        # The SDK caches locally and only raises when there's no cached copy
        # AND the network call fails, so a transient blip usually returns a
        # stale cached prompt rather than reaching here.
        prompt_obj = lf.get_prompt(prompt_name)
        return prompt_obj.prompt, f"{prompt_name}@{prompt_obj.version}"

    except NotFoundError:
        # Case 1: the prompt really doesn't exist. Register the current
        # hardcoded version as the baseline so future edits are tracked.
        # Read the version back off the created object instead of assuming
        # @1 — if this name existed before and lost its labels, the new
        # version could be higher than 1.
        try:
            created = lf.create_prompt(
                name=prompt_name,
                prompt=fallback,
                labels=["production"],
            )
            version = getattr(created, "version", None)
            label = f"{prompt_name}@{version}" if version is not None else f"{prompt_name}@unknown"
            logger.info("Registered baseline prompt in Langfuse: %s", label)
            return fallback, label
        except Exception as create_exc:
            logger.warning("Could not register prompt in Langfuse: %s", create_exc)
            return fallback, "hardcoded-register-failed"

    except Exception as lookup_exc:
        # Case 2: lookup failed for a reason other than not-found. Do NOT
        # create a prompt. Return the fallback text with a label that says
        # plainly the lookup failed, so the artifact records the truth
        # rather than a fabricated version.
        logger.warning(
            "Langfuse prompt lookup failed for '%s' (not creating a new version): %s",
            prompt_name,
            lookup_exc,
        )
        return fallback, "hardcoded-lookup-failed"


def get_system_prompt(fallback: str) -> tuple[str, str]:
    """
    Fetch the active research system prompt from Langfuse if available.

    On first use, the fallback (hardcoded) prompt is registered in Langfuse
    under "pm-research-system-prompt". Subsequent edits in the Langfuse UI
    create new versions, and this returns the version labeled "production".

    See _resolve_versioned_prompt for the full list of returned version
    labels, including the failure labels.
    """
    return _resolve_versioned_prompt("pm-research-system-prompt", fallback)


def get_eval_prompt(eval_name: str, fallback: str) -> tuple[str, str]:
    """
    Fetch a versioned eval prompt from Langfuse if available.

    eval_name is the short dimension name (question_coverage, groundedness,
    synthesis_quality). It's registered under "pm-research-eval-<name>" so
    eval prompts sit in their own namespace, distinct from the research
    system prompt.

    On first use the fallback (the text loaded from evals/prompts/<name>.md)
    is registered as the baseline. Later edits in the Langfuse UI create new
    versions. This is what lets a change to the grading criteria be tracked
    alongside changes to the research prompt.

    Returns (prompt_text, version_label), same shape and same failure labels
    as get_system_prompt.
    """
    return _resolve_versioned_prompt(f"pm-research-eval-{eval_name}", fallback)
