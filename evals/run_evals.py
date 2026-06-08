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
        messages=[{"role": "user", "content": prompt}],
    )
    text = response.content[0].text.strip()

    # Strip ```json ... ``` fences if the model included them despite instructions.
    if text.startswith("```"):
        lines = text.splitlines()
        # Drop opening fence line (```json or ```) and closing fence line (```)
        text = "\n".join(lines[1:-1]).strip()

    return json.loads(text)


def run_evals(run_dir: Path, skip_langfuse: bool = False) -> EvalScores:
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
    # We only include tool calls that actually retrieved content (not Notion writes).
    retrieval_tools = {"web_search", "drive_read_document", "drive_list_files"}
    source_parts = []
    for tc in tool_calls:
        if tc["tool"] in retrieval_tools:
            source_parts.append(
                f"[{tc['tool']}] input={json.dumps(tc['input'])} "
                f"preview={tc['result_preview']}"
            )
    source_material = "\n\n".join(source_parts) if source_parts else "(no retrieval results recorded)"

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
    print("COVERAGE FULL:", json.dumps(coverage_result, indent=2))

    # --- Groundedness -------------------------------------------------------
    groundedness_result = _run_eval(
        client,
        _load_prompt("groundedness")
        	.replace("{source_material}", source_material)
        	.replace("{brief}", brief),
    )
    print(f"  Groundedness     : {groundedness_result['score']}/5")
    print("GROUNDEDNESS FULL:", json.dumps(groundedness_result, indent=2))

    # --- Synthesis quality --------------------------------------------------
    synthesis_result = _run_eval(
        client,
        _load_prompt("synthesis_quality")
        	.replace("{brief}", brief),
    )
    print(f"  Synthesis quality: {synthesis_result['score']}/5")
    print("SYNTHESIS FULL:", json.dumps(synthesis_result, indent=2))

    scores = EvalScores(
        question_coverage=coverage_result["score"],
        groundedness=groundedness_result["score"],
        synthesis_quality=synthesis_result["score"],
        question_coverage_reasoning=coverage_result.get("reasoning", ""),
        groundedness_reasoning=groundedness_result.get("reasoning", ""),
        synthesis_quality_reasoning=synthesis_result.get("reasoning", ""),
    )

    # --- Post to Langfuse ---------------------------------------------------
    if not skip_langfuse and langfuse_trace_id:
        _post_langfuse_scores(scores, langfuse_trace_id)
    elif not skip_langfuse and not langfuse_trace_id:
        print("  Langfuse: no trace ID in artifact — scores not posted")

    return scores


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
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("Error: ANTHROPIC_API_KEY is not set.")
        sys.exit(1)

    run_dir = Path(args.run_dir)
    scores = run_evals(run_dir, skip_langfuse=args.skip_langfuse)

    print("\nEval summary:")
    print(f"  Question coverage : {scores.question_coverage}/5")
    print(f"  Groundedness      : {scores.groundedness}/5")
    print(f"  Synthesis quality : {scores.synthesis_quality}/5")


if __name__ == "__main__":
    main()
