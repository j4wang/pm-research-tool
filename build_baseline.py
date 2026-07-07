"""
build_baseline.py

Generate the data section of baseline.md from a set of run dirs. Reads each
run's result.json (topic, model, system prompt version, template) and
eval_scores.json (the three scores and the eval prompt versions), then emits
a markdown table plus a provenance block.

This writes the data only. The narrative framing at the top of baseline.md
(why these templates, what the baseline means) is written by hand once and
left alone. Re-running this generator refreshes the table without touching
your framing, as long as you keep the framing above the marker line.

Usage:
  python build_baseline.py \
      --run-dir runs/<competitive> \
      --run-dir runs/<deep> \
      --run-dir runs/<startup> \
      --label baseline \
      --out baseline_data.md

Then paste the output under your hand-written framing in baseline.md, or
point --out straight at a file you include from baseline.md.
"""

import argparse
import json
from pathlib import Path


def load_run(run_dir: Path) -> dict:
    result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
    scores_path = run_dir / "eval_scores.json"
    if not scores_path.exists():
        raise FileNotFoundError(f"No eval_scores.json in {run_dir}. Run evals against it first.")
    scores_doc = json.loads(scores_path.read_text(encoding="utf-8"))
    latest = scores_doc["latest"]
    return {
        "run_dir": str(run_dir),
        "topic": result.get("topic", ""),
        "model": result.get("model", ""),
        "system_prompt_version": result.get("prompt_version", ""),
        "template": result.get("question_template", ""),
        "eval_model": latest.get("eval_model", ""),
        "eval_prompt_versions": latest.get("eval_prompt_versions", {}),
        "scores": latest.get("scores", {}),
        "scored_at": latest.get("scored_at", ""),
    }


def render(runs: list[dict], label: str) -> str:
    # Sanity: warn in-band if provenance isn't consistent across runs, since a
    # baseline is only meaningful if the model and prompt version are constant.
    models = {r["model"] for r in runs}
    sys_versions = {r["system_prompt_version"] for r in runs}
    topics = {r["topic"] for r in runs}
    eval_models = {r["eval_model"] for r in runs}

    lines = []
    lines.append(f"## {label} scores")
    lines.append("")
    lines.append(f"Generated from {len(runs)} run(s). Topic held constant across all runs.")
    lines.append("")

    # Provenance block.
    lines.append("### Provenance")
    lines.append("")
    lines.append(f"- Topic: {', '.join(sorted(topics))}")
    lines.append(f"- Research model: {', '.join(sorted(models))}")
    lines.append(f"- System prompt: {', '.join(sorted(sys_versions))}")
    lines.append(f"- Eval model: {', '.join(sorted(eval_models))}")
    lines.append("")

    warnings = []
    if len(models) > 1:
        warnings.append("Research model is NOT constant across runs. Baseline is confounded.")
    if len(sys_versions) > 1:
        warnings.append("System prompt version is NOT constant across runs. Baseline is confounded.")
    if len(topics) > 1:
        warnings.append("Topic is NOT constant across runs. Baseline is confounded.")
    if len(eval_models) > 1:
        warnings.append("Eval model is NOT constant across runs. Scores are not comparable.")
    if warnings:
        lines.append("### WARNINGS")
        lines.append("")
        for w in warnings:
            lines.append(f"- {w}")
        lines.append("")

    # Scores table.
    lines.append("### Scores by template")
    lines.append("")
    lines.append("| Template | Coverage | Groundedness | Synthesis | Run dir |")
    lines.append("|---|---|---|---|---|")
    for r in runs:
        s = r["scores"]
        lines.append(
            f"| {r['template']} | {s.get('question_coverage','?')} | "
            f"{s.get('groundedness','?')} | {s.get('synthesis_quality','?')} | "
            f"`{r['run_dir']}` |"
        )
    lines.append("")

    # Eval prompt versions. Recorded so a score can be traced to grading criteria.
    lines.append("### Eval prompt versions")
    lines.append("")
    lines.append("The version of each judge prompt used to produce the scores above.")
    lines.append("")
    for r in runs:
        v = r["eval_prompt_versions"]
        lines.append(f"- `{r['template']}`:")
        for dim in ("question_coverage", "groundedness", "synthesis_quality"):
            lines.append(f"    - {dim}: {v.get(dim, '?')}")
    lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-dir", "-r", action="append", required=True, help="Run dir (repeatable)")
    parser.add_argument("--label", default="Baseline", help="Section label, e.g. 'Baseline' or 'Post-regression'")
    parser.add_argument("--out", "-o", help="Write markdown here. Prints to stdout if omitted.")
    args = parser.parse_args()

    runs = [load_run(Path(d)) for d in args.run_dir]
    md = render(runs, args.label)

    if args.out:
        Path(args.out).write_text(md, encoding="utf-8")
        print(f"Wrote {args.out}")
    else:
        print(md)


if __name__ == "__main__":
    main()
