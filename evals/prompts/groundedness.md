You are evaluating whether the claims in a research brief are traceable to retrieved source material.

Source material retrieved during research (search result previews and document snippets):
{source_material}

Research brief:
{brief}

Assess how well the claims in the brief are grounded in the provided source material:
- Are the key factual claims traceable to at least one source?
- Does the brief assert things that go meaningfully beyond what the sources support?
- Are there significant claims with no apparent source in the retrieved material?

Note: synthesis, inference, and drawing conclusions across sources is expected and good.
Penalize only claims that appear invented or contradict the source material.

Assign a groundedness score from 1 to 5:
1 = Many key claims appear unsupported or contradict the sources
2 = Some key claims lack grounding in retrieved material
3 = Most claims are grounded, a few unsupported assertions present
4 = Claims are well-grounded, minor speculation clearly signaled
5 = All substantive claims are traceable to retrieved sources

Respond ONLY with a valid JSON object in this exact format, with no preamble, explanation, or markdown fences:
{
  "score": <integer 1-5>,
  "reasoning": "<one paragraph explaining the score>",
  "ungrounded_claims": ["<claim 1>", "<claim 2>"]
}
