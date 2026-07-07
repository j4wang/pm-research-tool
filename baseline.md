# Eval Baseline and Staged Regression

## What this is
This records a baseline eval capture and a deliberately staged regression. The point was to confirm the eval harness does more than run. It catches a bad prompt change before that change ships.

Building evals and building evals that catch something are different claims. This documents the second one.

## Setup
The topic was held constant across every run. It was Upstart. Holding the topic fixed means the only thing that changes between baseline and regression is the prompt. Search noise stays roughly constant on both sides.

The research model was claude-sonnet-4-6, pinned for all runs. The judge model was claude-haiku-4-5. Scores are integers from 1 to 5. There are three dimensions. Coverage. Groundedness. Synthesis.

The regression was staged in Langfuse, not in code. The system prompt was version 1 at baseline. A weakened version 2 was created in Langfuse for the regression. Version 2 told the model to answer only the questions it judged most important and to keep the brief short. The production label was moved to version 2 for the regression runs. It was moved back to version 1 afterward.

## Baseline scores (system prompt v1)
| Template | Coverage | Groundedness | Synthesis | Run |
|---|---|---|---|---|
| competitive.md | 4 | 3 | 3 | `runs/20260707_010359` |
| competitive-deep.md | 4 | 3 | 4 | `runs/20260707_010904` |
| competitive-startup.md | 2 | 2 | 4 | `runs/20260707_011405` |

## Regression scores (system prompt v2, degraded)
| Template | Coverage | Groundedness | Synthesis | Run |
|---|---|---|---|---|
| competitive.md | 3 | 2 | 3 | `runs/20260707_030034` |
| competitive-deep.md | 2 | 2 | 4 | `runs/20260707_030352` |

## What the regression showed
The deep template is the clean catch. Coverage fell from 4 to 2. That is a two point drop tied to a single prompt version change. The judge reasoning on the degraded run described a thin brief. It read as a market summary rather than a competitive intelligence document. The harness caught the regression.

The competitive template is a weaker signal. Coverage fell one point, from 4 to 3. The same template scored both 3 and 4 on undegraded runs during earlier testing. A one point move sits inside that run-to-run variance. So the drop is real but not conclusive on its own. The judge reasoning on the degraded run still described a full structured brief. The model largely ignored the instruction to keep the brief short.

The template dependence is the actual finding. On harder research questions the weakened prompt caused real coverage damage. On simpler questions the model compensated and produced a near-normal brief. A harness that averaged the two templates into one number would have hidden this.

## The startup template
competitive-startup.md scored coverage 2 at baseline, before any degradation. The judge found the brief covered Upstart alone. It never compared competitors, which the template explicitly asks for. This is a real tool limitation, not a judge error. The research loop does not reliably gather founder and funding data for every competitor.

The template was excluded from the regression test. A coverage score already at 2 has almost no room left to drop, so it can't show a regression cleanly.

## Judge stability
Before capturing the baseline, the judge was checked for run-to-run stability. The eval suite was run three times against one fixed brief. All three scores came back identical at the score level. The reasoning text drifted slightly at the sub-item level, but the scores held. The judge temperature was set to 0 to remove sampling drift.

The judge was also checked for discrimination. A brief was deliberately gutted by removing its competitor table. Coverage dropped from 3 to 2 while groundedness and synthesis held. This confirmed the judge can score below the middle of the scale and moves for the right reason.

## Eval prompt versions
All scores above were produced with these judge prompt versions.
- question_coverage: pm-research-eval-question_coverage@1
- groundedness: pm-research-eval-groundedness@1
- synthesis_quality: pm-research-eval-synthesis_quality@1

These are versioned in Langfuse alongside the system prompt, so a change to grading criteria is tracked the same way a change to the research prompt is.
