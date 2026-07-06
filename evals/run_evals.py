"""
evals/run_evals.py
Runs the three-dimensional eval suite against a completed research run artifact.

Each eval calls Claude as a judge (LLM-as-judge pattern) using a prompt from
evals/prompts/. Scores are integers 1-5. Results are posted to Langfuse against
the trace ID stored in the run artifact, linking eval scores back to the run
that produced them.

Usage:
  # Run automatically after research.py (default behavior)
  python research.py --questions questions/competitive.md --topic "AI note-taking apps"

  # Run manually against a completed run (useful when iterating on eval prompts)
  python -m evals.run_evals --run-dir runs/20250531_143022

  # Run without posting to Langfuse (local testing)
  python -m evals.run_evals --run-dir runs/20250531_143022 --skip-langfuse

Environment variables required (set in .env):
  ANTHROPIC_API_KEY
  LANGFUSE_PUBLIC_KEY   (only needed when posting scores)
  LANGFUSE_SECRET_KEY   (only needed when posting scores)
  LANGFUSE_HOST         (optional; defaults to https://cloud.langfuse.com)
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import anthropic
from dotenv import load_dotenv

load_dotenv()

# Use a cheaper model for evals — judgment quality doesn't require Sonnet.
EVAL_MODEL = "claude-haiku-4-5-20251001"
PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class EvalScores:
    question_coverage: int
    groundedness: int
    synthesis_quality: int
    question_coverage_reasoning: str
    groundedness_reasoning: str
    synthesis_quality_reasoning: str


# ---------------------------------------------------------------------------
# Eval execution
# ---------------------------------------------------------------------------

def _load_prompt(name: str) -> str:
    """Load an eval prompt template from evals/prompts/<name>.md."""
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def _run_eval(client: anthropic.Anthropic, prompt: str) -> dict:
    """
    Send an eval prompt to Claude and parse the JSON response.

    Each eval prompt instructs the model to respond with only a JSON object.
    This strips any accidental markdown fences before parsing.
    """
    response = client.messages.create(
        model=EVAL_MODEL,
        max_tokens=1000,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    # Strip ```json ... ``` fences if the model included them despite instructions.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence line (```json or ```) and closing fence line (```)
        text = "\n".join(lines[1:-1]).strip()

    return json.loads(text)


def run_evals(
    run_dir: Path,
    skip_langfuse: bool = False,
    append_scores: bool = False,
) -> EvalScores:
    """
    Run the eval suite against a completed research run.

    Loads result.json from run_dir, runs three evals against Claude, and
    posts scores to Langfuse (unless skip_langfuse is True).

    Can be called from research.py after a run completes, or from the CLI
    to re-score a historical run without re-running the research itself.
    """
    result_path = run_dir / "result.json"
    if not result_path.exists():
        raise FileNotFoundError(f"No result.json found in {run_dir}")

    result = json.loads(result_path.read_text(encoding="utf-8"))

    brief = result["brief"]
    questions = result.get("questions", "")
    tool_calls = result.get("tool_calls", [])
    langfuse_trace_id = result.get("langfuse_trace_id")

    # Build a source material string for the groundedness eval.
    #
    # We exclude rather than include. The MCP servers are discovered at
    # runtime, so a hardcoded include-list drifts out of sync the moment a
    # server's tool names change. notion_create_page is the one write-only
    # tool with no retrieved content, so it's the thing we name explicitly.
    #
    # We also drop errored calls. A failed tool returns error text, not
    # source material, and the judge has no way to tell those apart from
    # real content unless we filter it out here.
    write_only_tools = {"notion_create_page"}
    excluded_error_count = 0
    source_parts = []
    for tc in tool_calls:
        if tc["tool"] in write_only_tools:
            continue
        if tc.get("is_error"):
            excluded_error_count += 1
            continue
        source_parts.append(
            f"[{tc['tool']}] input={json.dumps(tc['input'])} "
            f"preview={tc['result_preview']}"
        )

    if source_parts:
        source_material = "\n\n".join(source_parts)
        if excluded_error_count:
            source_material += (
                f"\n\n(Note: {excluded_error_count} tool call(s) failed during this "
                f"run and are excluded from the source material above.)"
            )
    else:
        source_material = "(no retrieval results recorded)"

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    print("Running evals...")

    # --- Question coverage --------------------------------------------------
    coverage_result = _run_eval(
        client,
        _load_prompt("question_coverage")
        	.replace("{questions}", questions)
        	.replace("{brief}", brief),
    )
    print(f"  Coverage         : {coverage_result['score']}/5")

    # --- Groundedness -------------------------------------------------------
    groundedness_result = _run_eval(
        client,
        _load_prompt("groundedness")
        	.replace("{source_material}", source_material)
        	.replace("{brief}", brief),
    )
    print(f"  Groundedness     : {groundedness_result['score']}/5")

    # --- Synthesis quality --------------------------------------------------
    synthesis_result = _run_eval(
        client,
        _load_prompt("synthesis_quality")
        	.replace("{brief}", brief),
    )
    print(f"  Synthesis quality: {synthesis_result['score']}/5")

    scores = EvalScores(
        question_coverage=coverage_result["score"],
        groundedness=groundedness_result["score"],
        synthesis_quality=synthesis_result["score"],
        question_coverage_reasoning=coverage_result.get("reasoning", ""),
        groundedness_reasoning=groundedness_result.get("reasoning", ""),
        synthesis_quality_reasoning=synthesis_result.get("reasoning", ""),
    )

    # --- Write scores into the run dir --------------------------------------
    # Always happens, independent of Langfuse. This is the durable,
    # reviewable record: a committed file a reviewer can open without a
    # Langfuse login.
    _write_scores_file(run_dir, scores, overwrite=not append_scores)

    # --- Post to Langfuse ---------------------------------------------------
    if not skip_langfuse and langfuse_trace_id:
        _post_langfuse_scores(scores, langfuse_trace_id)
    elif not skip_langfuse and not langfuse_trace_id:
        print("  Langfuse: no trace ID in artifact — scores not posted")

    return scores


def _write_scores_file(
    run_dir: Path,
    scores: EvalScores,
    overwrite: bool = True,
) -> Path:
    """
    Write eval scores to run_dir/eval_scores.json.

    Deliberately a separate file from result.json. result.json is the
    research artifact written by research.py. Eval scores come from a
    later, separate scoring pass. Keeping them apart means re-scoring a
    run never rewrites the original research record, so provenance stays
    clean and you can always tell what the run produced versus what a
    later eval pass added.

    The eval model and a timestamp are recorded alongside the scores. A
    baseline captured on one eval model and a comparison run on another
    would otherwise look identical in the file and mislead you. This is
    the same model-confounding trap the ANTHROPIC_MODEL override creates
    on the research side.

    When overwrite is False and a scores file already exists, the new
    scores are appended to a history list instead of replacing the file.
    That's what lets repeated eval passes against one run dir accumulate
    rather than clobber each other.
    """
    from datetime import datetime, timezone

    scores_path = run_dir / "eval_scores.json"

    entry = {
        "eval_model": EVAL_MODEL,
        "scored_at": datetime.now(timezone.utc).isoformat(),
        "scores": {
            "question_coverage": scores.question_coverage,
            "groundedness": scores.groundedness,
            "synthesis_quality": scores.synthesis_quality,
        },
        "reasoning": {
            "question_coverage": scores.question_coverage_reasoning,
            "groundedness": scores.groundedness_reasoning,
            "synthesis_quality": scores.synthesis_quality_reasoning,
        },
    }

    if overwrite or not scores_path.exists():
        payload = {"latest": entry, "history": [entry]}
        scores_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    else:
        existing = json.loads(scores_path.read_text(encoding="utf-8"))
        history = existing.get("history", [])
        history.append(entry)
        payload = {"latest": entry, "history": history}
        scores_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    print(f"  Scores written to {scores_path}")
    return scores_path


def _post_langfuse_scores(scores: EvalScores, trace_id: str) -> None:
    try:
        from observability import get_langfuse
        lf = get_langfuse()
    except (ImportError, RuntimeError):
        from langfuse import get_client
        lf = get_client()

    lf.create_score(
        trace_id=trace_id,
        name="question_coverage",
        value=float(scores.question_coverage),
        data_type="NUMERIC",
        comment=scores.question_coverage_reasoning,
    )
    lf.create_score(
        trace_id=trace_id,
        name="groundedness",
        value=float(scores.groundedness),
        data_type="NUMERIC",
        comment=scores.groundedness_reasoning,
    )
    lf.create_score(
        trace_id=trace_id,
        name="synthesis_quality",
        value=float(scores.synthesis_quality),
        data_type="NUMERIC",
        comment=scores.synthesis_quality_reasoning,
    )
    lf.flush()
    print(f"  Langfuse: scores posted (trace: {trace_id})")

# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Run the eval suite against a completed research run artifact.",
        epilog=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--run-dir", "-r",
        required=True,
        help="Path to the run directory containing result.json. E.g. runs/20250531_143022",
    )
    parser.add_argument(
        "--skip-langfuse",
        action="store_true",
        help="Skip posting scores to Langfuse (useful for local testing).",
    )
    parser.add_argument(
        "--append-scores",
        action="store_true",
        help=(
            "Append to eval_scores.json instead of overwriting it. Use this "
            "for repeated eval passes against one run dir (e.g. a variance "
            "check) when you want each pass kept rather than clobbered."
        ),
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    run_dir = Path(args.run_dir)
    scores = run_evals(
        run_dir,
        skip_langfuse=args.skip_langfuse,
        append_scores=args.append_scores,
    )

    print("\nEval summary:")
    print(f"  Question coverage : {scores.question_coverage}/5")
    print(f"  Groundedness      : {scores.groundedness}/5")
    print(f"  Synthesis quality : {scores.synthesis_quality}/5")


if __name__ == "__main__":
    main()
