You are evaluating a research brief for completeness against the original research questions.

Research questions:
{questions}

Research brief:
{brief}

For each research question, assess whether the brief addresses it:
- fully addressed: the brief contains a clear, substantive answer with supporting detail
- partially addressed: the brief touches on it but lacks depth, specificity, or evidence
- not addressed: the brief does not answer this question at all

Then assign an overall coverage score from 1 to 5:
1 = Most questions not addressed
2 = Some questions addressed, significant gaps remain
3 = Most questions addressed, a few gaps
4 = All questions addressed, minor gaps in depth
5 = All questions thoroughly and specifically addressed

Respond ONLY with a valid JSON object in this exact format, with no preamble, explanation, or markdown fences:
{
  "score": <integer 1-5>,
  "reasoning": "<one paragraph explaining the score>",
  "question_breakdown": [
    {
      "question": "<question text, abbreviated if long>",
      "status": "<fully addressed|partially addressed|not addressed>",
      "notes": "<one sentence>"
    }
  ]
}
