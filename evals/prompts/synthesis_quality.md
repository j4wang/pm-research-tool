You are evaluating the synthesis quality of a research brief.

Research brief:
{brief}

Assess whether the brief synthesizes information or merely summarizes it source by source.
A high-quality synthesis:
- Draws conclusions that require combining or contrasting multiple sources
- Identifies patterns, tensions, or themes that no single source surfaces alone
- Surfaces implications relevant to product decisions
- Leads with what matters, not with a list of what was found

A low-quality synthesis:
- Paraphrases individual sources one after another without connection
- Reserves conclusions for a short closing paragraph while the body is a list of summaries
- Restates facts without interpreting their significance

Assign a synthesis quality score from 1 to 5:
1 = Pure summarization, no cross-source synthesis
2 = Mostly summarization with occasional connecting observations
3 = Mix of summarization and synthesis
4 = Predominantly synthetic, conclusions clearly drawn from multiple sources
5 = Highly synthetic throughout — surfaces non-obvious patterns and product implications

Respond ONLY with a valid JSON object in this exact format, with no preamble, explanation, or markdown fences:
{
  "score": <integer 1-5>,
  "reasoning": "<one paragraph explaining the score>"
}
