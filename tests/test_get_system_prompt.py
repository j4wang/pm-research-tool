"""
tests/test_get_system_prompt.py

Unit tests for observability.get_system_prompt.

These lock in the one behavior that's invisible in normal use and easy to
regress: a Langfuse lookup failure that is NOT a missing-prompt error must
never trigger create_prompt. The original bug caught every exception in one
branch and created a junk prompt version on any failure, including auth and
network errors. It also returned a fabricated "@1" version label that then
got written into the run artifact as fact.

The tests mock the Langfuse client, so they don't need a live Langfuse
account or network. They do need observability.py to be importable, which
means the phoenix and langfuse packages must be installed. If they aren't,
the whole module skips rather than failing, so this test file is safe to
run in a bare environment.

Run with:
  pytest tests/test_get_system_prompt.py
"""

import sys
import types

import pytest

# observability.py imports phoenix and langfuse at module top. In an
# environment without the full observability stack, that import fails.
# Skip the whole module in that case rather than erroring, so a bare
# checkout can still run the rest of the suite.
observability = pytest.importorskip(
    "observability",
    reason="observability.py needs phoenix + langfuse installed",
)


FALLBACK = "FALLBACK PROMPT TEXT"
PROMPT_NAME = "pm-research-system-prompt"


class _Obj:
    """Stand-in for a returned prompt object with arbitrary attributes."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _NotFoundError(Exception):
    """Stand-in for LangfuseNotFoundError."""
    pass


@pytest.fixture
def patched(monkeypatch):
    """
    Patch get_system_prompt's dependencies so each test can inject a fake
    Langfuse client and control which branch fires.

    Returns a helper that installs a given fake client (or a RuntimeError
    to simulate 'Langfuse not initialized') and returns a record of
    whether create_prompt was called.
    """
    # Make the runtime error-class resolution find our stand-in, so the
    # except NotFoundError branch is reachable in tests without the real
    # SDK's exception class.
    fake_errors = types.ModuleType("langfuse.errors")
    fake_errors.LangfuseNotFoundError = _NotFoundError
    monkeypatch.setitem(sys.modules, "langfuse.errors", fake_errors)

    def install(lf_client_or_exc):
        if isinstance(lf_client_or_exc, Exception):
            def _get_langfuse():
                raise lf_client_or_exc
        else:
            def _get_langfuse():
                return lf_client_or_exc
        monkeypatch.setattr(observability, "get_langfuse", _get_langfuse)

    return install


def test_happy_path_returns_fetched_version(patched):
    """Prompt exists. Return its text and its real version label."""
    class LF:
        def get_prompt(self, name):
            return _Obj(prompt="REAL PROMPT", version=3)

    patched(LF())
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == "REAL PROMPT"
    assert label == f"{PROMPT_NAME}@3"


def test_not_found_registers_and_reads_back_version(patched):
    """
    Prompt is missing. Register it, and read the version off the created
    object rather than assuming 1. A name that existed before and lost its
    labels can come back at version 2 or higher.
    """
    created_calls = []

    class LF:
        def get_prompt(self, name):
            raise _NotFoundError("404 not found")
        def create_prompt(self, **kwargs):
            created_calls.append(kwargs)
            return _Obj(version=2)

    patched(LF())
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == FALLBACK
    assert label == f"{PROMPT_NAME}@2"
    assert len(created_calls) == 1
    assert created_calls[0]["labels"] == ["production"]


def test_not_found_create_fails_returns_honest_label(patched):
    """Prompt missing and registration also fails. Honest label, fallback text."""
    class LF:
        def get_prompt(self, name):
            raise _NotFoundError("404 not found")
        def create_prompt(self, **kwargs):
            raise RuntimeError("write blocked")

    patched(LF())
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == FALLBACK
    assert label == "hardcoded-register-failed"


def test_lookup_failure_does_not_create_prompt(patched):
    """
    The load-bearing test. A non-not-found lookup failure (auth, hard
    network) must return the fallback with an honest label and must NOT
    call create_prompt. This is the exact regression the fix prevents.
    """
    created_calls = []

    class LF:
        def get_prompt(self, name):
            raise RuntimeError("401 unauthorized")
        def create_prompt(self, **kwargs):
            created_calls.append(kwargs)
            return _Obj(version=99)

    patched(LF())
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == FALLBACK
    assert label == "hardcoded-lookup-failed"
    assert created_calls == []


def test_langfuse_not_initialized_returns_hardcoded(patched):
    """Langfuse never initialized. Plain 'hardcoded' label, fallback text."""
    patched(RuntimeError("Observability not initialized"))
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == FALLBACK
    assert label == "hardcoded"


def test_created_object_without_version_attribute(patched):
    """
    Defensive path. If create_prompt returns an object with no .version
    attribute, the label falls back to '@unknown' rather than crashing.
    This guards against an SDK return-shape change.
    """
    class LF:
        def get_prompt(self, name):
            raise _NotFoundError("404 not found")
        def create_prompt(self, **kwargs):
            return _Obj()  # no version attribute

    patched(LF())
    text, label = observability.get_system_prompt(FALLBACK)

    assert text == FALLBACK
    assert label == f"{PROMPT_NAME}@unknown"
