"""What the interviewer is told, and what the room asks the model to write.

Three kinds of call, three registers:

* **The brief** — a structured call. Invent a company (or a coding brief
  on top of one), or generate a consulting case. JSON against a schema,
  read by the page, never spoken.
* **The interview** — a conversation. Everything the interviewer says is
  synthesized and heard, so the speech rules from Ciel's own prompt apply
  in spirit: no markdown, one question at a time, sentences a voice can
  carry. The interviewer stays in character, and breaks it for exactly
  one thing: to apologize for having cut the candidate off.
* **The debrief** — a structured call again, at higher effort, over the
  whole transcript. Read, not heard.

The interviewer reaches the page through two directives on lines of
their own — ``[[exhibit: e1]]`` shares a case exhibit, ``[[problem: p1]]``
poses a coding problem from the brief. The room strips them from speech
and turns them into frames; the model never has to produce a table or a
code block out loud.
"""

from __future__ import annotations

import json
import re
import textwrap
from typing import Any

MODES = ("company", "case", "technical")

CUTOFF_MARKER = (
    "[The candidate was interrupted before they had finished. Disregard the "
    "reply you were giving and do not repeat it. Say one short apology, then "
    "respond to their complete answer, which follows.]"
)
BEGIN = "[begin]"
END = "[end]"
CODE_SUBMITTED = "[The candidate has submitted their code. Review it as an interviewer would: correctness first, then complexity, then edge cases and style. Ask, do not lecture.]"

DIRECTIVE = re.compile(r"\[\[(exhibit|problem):\s*([A-Za-z0-9_-]+)\]\]")

LANGUAGES = ("python", "c", "cpp", "java", "rust", "javascript", "typescript", "go")

# ── schemas ──────────────────────────────────────────────────────────────────


def _s(type_: str, **kw: Any) -> dict[str, Any]:
    return {"type": type_, **kw}


def _obj(props: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required if required is not None else list(props),
        "additionalProperties": False,
    }


def _arr(items: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": items}


_STR = _s("string")

COMPANY_SCHEMA = _obj({
    "name": _STR,
    "tagline": _STR,
    "industry": _STR,
    "stage": _STR,
    "size": _STR,
    "hq": _STR,
    "founded": _STR,
    "products": _arr(_STR),
    "business_model": _STR,
    "recent_news": _arr(_STR),
    "culture": _STR,
})

ROLE_SCHEMA = _obj({
    "title": _STR,
    "level": _STR,
    "team": _STR,
    "responsibilities": _arr(_STR),
    "must_haves": _arr(_STR),
})

INTERVIEWER_SCHEMA = _obj({"name": _STR, "title": _STR, "style": _STR})

QUESTION_SCHEMA = _obj({
    "q": _STR,
    "kind": _s("string", enum=["behavioural", "technical", "product", "case", "role"]),
})

COMPANY_BRIEF_SCHEMA = {
    "title": "company_brief",
    **_obj({
        "company": COMPANY_SCHEMA,
        "role": ROLE_SCHEMA,
        "interviewer": INTERVIEWER_SCHEMA,
        "likely_questions": _arr(QUESTION_SCHEMA),
        "hidden_notes": _arr(_STR),
    }),
}

PROBLEM_SCHEMA = _obj({
    "id": _STR,
    "title": _STR,
    "topic": _STR,
    "difficulty": _s("string", enum=["warm-up", "medium", "hard"]),
    "statement": _STR,
    "examples": _arr(_obj({"input": _STR, "output": _STR, "note": _STR})),
    "constraints": _arr(_STR),
    "starter": _obj({lang: _STR for lang in LANGUAGES}),
    "reference_notes": _STR,
    "follow_up": _STR,
})

TECHNICAL_BRIEF_SCHEMA = {
    "title": "technical_brief",
    **_obj({
        "company": COMPANY_SCHEMA,
        "role": ROLE_SCHEMA,
        "interviewer": INTERVIEWER_SCHEMA,
        "likely_questions": _arr(QUESTION_SCHEMA),
        "hidden_notes": _arr(_STR),
        "technical": _obj({
            "focus": _STR,
            "what_to_expect": _arr(_STR),
            "problems": _arr(PROBLEM_SCHEMA),
        }),
    }),
}

EXHIBIT_SCHEMA = _obj({
    "id": _STR,
    "title": _STR,
    "kind": _s("string", enum=["table", "text"]),
    "columns": _arr(_STR),
    "rows": _arr(_arr(_STR)),
    "note": _STR,
    "reveal_when": _STR,
})

CASE_SCHEMA = {
    "title": "case",
    **_obj({
        "slug": _STR,
        "title": _STR,
        "type": _s("string", enum=["product", "project", "company"]),
        "client": _STR,
        "year": _s("integer"),
        "prompt": _STR,
        "background": _STR,
        "clarifications": _arr(_obj({"q": _STR, "a": _STR})),
        "exhibits": _arr(EXHIBIT_SCHEMA),
        "framework_hints": _arr(_STR),
        "good_answer": _STR,
        "historical_outcome": _STR,
        "sources": _arr(_STR),
    }),
}

DEBRIEF_SCHEMA = {
    "title": "debrief",
    **_obj({
        "overall": _STR,
        "score_1_5": _s("integer"),
        "strengths": _arr(_STR),
        "improvements": _arr(_STR),
        "per_question": _arr(_obj({"q": _STR, "note": _STR, "score_1_5": _s("integer")})),
        "communication": _obj({"clarity": _STR, "structure": _STR, "pace": _STR}),
        "case": _obj({
            "recommendation_given": _STR,
            "vs_historical_outcome": _STR,
            "structure_score_1_5": _s("integer"),
        }),
        "code": _obj({
            "correctness": _STR,
            "complexity": _STR,
            "style": _STR,
            "score_1_5": _s("integer"),
        }),
        "next_practice": _arr(_STR),
    }),
}


def brief_schema(mode: str) -> dict[str, Any]:
    return {
        "company": COMPANY_BRIEF_SCHEMA,
        "technical": TECHNICAL_BRIEF_SCHEMA,
        "case": CASE_SCHEMA,
    }[mode]


# ── the brief ────────────────────────────────────────────────────────────────

_BRIEF_SYSTEM = """\
You design realistic mock interviews. You write structured briefs that a \
web page will display to a candidate before their practice interview, and \
that an interviewer persona will later be given as the truth of the world.

Everything you invent is fictional and must not be a real company, a real \
person, or a real trademark. Names should sound plausible and be internally \
consistent — a fintech founded in twenty nineteen does not have forty \
thousand employees. Write the way a good recruiter's prep pack reads: \
concrete, specific, no filler. Recent news should be the kind of thing a \
candidate could bring up in the interview. Hidden notes are facts the \
interviewer knows and the brief withholds — a recent outage, a reorg, a \
metric that matters — so the interviewer has something to probe with.\
"""

_CASE_SYSTEM = """\
You write consulting case interviews the way the top firms run them: a \
short prompt, background the candidate must ask for, exhibits handed over \
at the right moment, and a real answer.

The case is grounded in a real, publicly documented business situation and \
its actual outcome — a market entry, a pricing change, a product launch, an \
operations turnaround — but the client is renamed and the numbers are \
rounded so the candidate reasons rather than remembers. The exhibits are \
small tables with real structure: two to five columns, three to eight rows, \
numbers that reward being read carefully (a segment that is growing while \
the total shrinks, a cost line that does not scale). Say in reveal_when \
what the candidate must ask or conclude before each exhibit is shown. \
The historical outcome states plainly what the real company did and how it \
went. Sources are public URLs.\
"""


def brief_request(mode: str, setup: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    """(system prompt, user message, schema) for the structured brief call."""
    length = int(setup.get("length_min") or 30)
    if mode == "case":
        case_type = setup.get("case_type") or "any"
        wanted = (
            "any type" if case_type == "any" else f"a {case_type} case "
            + {
                "product": "(a product decision: launch, pricing, positioning, or kill)",
                "project": "(an operations or delivery problem: a turnaround, a rollout, a cost programme)",
                "company": "(a company-level strategy question: market entry, growth, M and A, profitability)",
            }.get(case_type, "")
        )
        user = (
            f"Generate one consulting case, {wanted}, sized for a {length}-minute "
            f"interview ({'two' if length <= 20 else 'three to four'} exhibits). "
            f"Candidate's request, if any: {setup.get('request') or 'none'}. "
            f"Return the case as JSON matching the schema."
        )
        return _CASE_SYSTEM, user, CASE_SCHEMA

    request = setup.get("request") or "a general software role"
    role = setup.get("role") or "unspecified"
    seniority = setup.get("seniority") or "unspecified"
    n_questions = 6 if length <= 15 else 10 if length <= 30 else 14
    if mode == "technical":
        focus = setup.get("focus") or "algorithms and data structures"
        user = textwrap.dedent(f"""\
            Design a technical interview brief.
            Candidate's request: {request}
            Role: {role}. Seniority: {seniority}. Length: {length} minutes.
            Technical focus: {focus}.

            Invent the company and the role, name the interviewer (an engineer),
            and list likely conversational questions (a few about background and
            the role). Then write {'one' if length <= 30 else 'two'} coding
            problem(s) sized for the time, with ids p1, p2…: a clear statement,
            two or three worked examples, constraints, starter code for every
            listed language (a function signature and a comment, nothing more),
            private reference notes on the intended solution and its complexity,
            and one follow-up variation. what_to_expect describes the shape of
            the coding part without revealing the problems.""")
        return _BRIEF_SYSTEM, user, TECHNICAL_BRIEF_SCHEMA

    user = textwrap.dedent(f"""\
        Design a company interview brief.
        Candidate's request: {request}
        Role: {role}. Seniority: {seniority}. Length: {length} minutes.

        Invent the company and the role, name the interviewer and their style,
        and list about {n_questions} likely questions matched to the role and
        the time — a mix of behavioural, role-specific, and where it fits,
        product or technical. Add hidden notes the interviewer can probe with.""")
    return _BRIEF_SYSTEM, user, COMPANY_BRIEF_SCHEMA


# ── the interview ────────────────────────────────────────────────────────────

_SPOKEN = """\
# How to speak

Everything you write is synthesized into speech and heard, not read. Never \
use markdown: no asterisks, bullets, numbered lists, headers, code blocks, or \
tables. Speak in complete, ordinary sentences, varied in length, the way a \
person across a table does. Spell out numbers the way you would say them. \
Never read out a URL or an identifier. Do not narrate yourself — no "Great \
question", no "Let me think". Keep each turn short: one point, then one \
question. Ask exactly one question at a time, and then stop and wait. \
Silence is the candidate thinking; you never fill it.\
"""

_CONDUCT = """\
# How to interview

Stay in character as the interviewer named below for the whole session. You \
are warm but professional, and you are evaluating. Follow up on specifics: \
when an answer is vague, ask for the concrete example, the number, the \
decision they personally made. When an answer is good, acknowledge it in a \
phrase and move on; do not gush. Do not give feedback or coaching during the \
interview — that comes afterwards, from someone else. Do not answer \
questions about how they are doing.

The candidate speaks through a microphone and their words reach you as a \
transcript, which may carry small transcription errors; read past them. A \
message that begins with a bracketed note in square brackets is an \
instruction from the room, not from the candidate: follow it, and never \
mention it or the brackets.

If a message begins with the note that the candidate was interrupted, you \
spoke over them: say one short, natural apology — "Sorry, go on." — and then \
respond to their complete answer as if you had heard it whole. Never \
apologize otherwise.

The message "[begin]" opens the interview: greet the candidate by the \
interviewer's name and role, in one or two sentences, and ask your first \
question. The message "[end]" closes it: thank them in one or two sentences \
and say the conversation is over; ask nothing further.\
"""

_COMPANY_MODE = """\
# The interview

This is a {length}-minute interview for the role below at the company below. \
Work through the likely questions in a natural order — you do not have to \
use them all, and you should follow interesting threads — and use the hidden \
notes to probe: they are things you know that the candidate's brief did not \
say. Keep an eye on time; with about a fifth of the time left, ask whether \
they have questions for you, answer briefly and in character from the \
company facts, and close.\
"""

_TECHNICAL_MODE = """\
# The interview

This is a {length}-minute technical interview for the role below. Open with \
one or two conversational questions about their background from the likely \
questions. Then pose the first coding problem by writing, on a line of its \
own, exactly:

[[problem: p1]]

That line is not spoken; the room shows the candidate the problem statement, \
the examples, the constraints, and a code editor with starter code. Before \
or after it, in speech, invite them to think aloud and ask clarifying \
questions. From then on the candidate may talk while they type; you see \
their code only when the room sends it to you (a message with a fenced code \
block), either as a snapshot while they work or when they submit. On a \
snapshot, say nothing about the code unless they asked; on a submission, \
review it as the note instructs. Give a hint only when they ask or are \
plainly stuck for a while, and make it a nudge, not the answer. Ask about \
time and space complexity once the solution is working. If time allows, \
pose the follow-up variation in speech, or the second problem with \
[[problem: p2]]. The room does not run code — you are reading it, as in a \
whiteboard interview; say so if they ask to run it.\
"""

_CASE_MODE = """\
# The interview

This is a {length}-minute consulting case interview. Open by reading the case \
prompt (in your own words, briefly). Let the candidate structure the problem \
before you give anything away; if they ask a clarifying question that the \
case's clarifications answer, give that answer; if they ask something the \
case does not cover, say the client does not have that information. When \
the candidate has asked for, or reasoned their way to, the data an exhibit's \
reveal_when describes, share it by writing, on a line of its own, exactly:

[[exhibit: e1]]

(with the exhibit's id). That line is not spoken; the room shows the table. \
Then ask what they make of it. Use the framework hints only to nudge a \
candidate who is stuck, never to lecture. Push on the arithmetic — ask for \
the number, and let them compute it. With a few minutes left, ask for a \
recommendation to the client in one minute: the answer, the reasoning, the \
risks. Do not reveal the historical outcome during the interview.\
"""


def _lines(items: Any) -> str:
    if not isinstance(items, list):
        return ""
    return "\n".join(f"- {item}" for item in items if item)


def interviewer_prompt(mode: str, brief: dict[str, Any], length_min: int) -> str:
    """The interviewer's system prompt for one session."""
    sections = [_SPOKEN, _CONDUCT]
    if mode == "case":
        sections.append(_CASE_MODE.format(length=length_min))
        exhibits = "\n\n".join(
            f"Exhibit {e.get('id')}: {e.get('title')}\nReveal when: {e.get('reveal_when')}\n"
            f"Columns: {', '.join(e.get('columns') or [])}\n"
            + "\n".join(" | ".join(str(c) for c in row) for row in (e.get("rows") or []))
            + (f"\nNote: {e.get('note')}" if e.get("note") else "")
            for e in brief.get("exhibits") or []
            if isinstance(e, dict)
        )
        clar = "\n".join(
            f"Q: {c.get('q')}\nA: {c.get('a')}" for c in brief.get("clarifications") or []
            if isinstance(c, dict)
        )
        sections.append(
            f"# The case\n\nTitle: {brief.get('title')}\nClient: {brief.get('client')} "
            f"(around {brief.get('year')})\n\nPrompt to read:\n{brief.get('prompt')}\n\n"
            f"Background you may share when asked:\n{brief.get('background')}\n\n"
            f"Clarifications:\n{clar}\n\n{exhibits}\n\n"
            f"Framework hints (for nudging only):\n{_lines(brief.get('framework_hints'))}\n\n"
            f"A good answer looks like:\n{brief.get('good_answer')}\n\n"
            f"Historical outcome (never reveal during the interview):\n{brief.get('historical_outcome')}"
        )
        return "\n\n".join(sections)

    company = brief.get("company") or {}
    role = brief.get("role") or {}
    who = brief.get("interviewer") or {}
    sections.append((_TECHNICAL_MODE if mode == "technical" else _COMPANY_MODE).format(length=length_min))
    sections.append(
        f"# You\n\nYou are {who.get('name')}, {who.get('title')} at {company.get('name')}. "
        f"Style: {who.get('style')}"
    )
    sections.append(
        f"# The company\n\n{company.get('name')} — {company.get('tagline')}\n"
        f"Industry: {company.get('industry')}. Stage: {company.get('stage')}. Size: {company.get('size')}. "
        f"HQ: {company.get('hq')}. Founded: {company.get('founded')}.\n"
        f"Products:\n{_lines(company.get('products'))}\n"
        f"Business model: {company.get('business_model')}\n"
        f"Recent news:\n{_lines(company.get('recent_news'))}\n"
        f"Culture: {company.get('culture')}"
    )
    sections.append(
        f"# The role\n\n{role.get('title')} ({role.get('level')}), on {role.get('team')}.\n"
        f"Responsibilities:\n{_lines(role.get('responsibilities'))}\n"
        f"Must-haves:\n{_lines(role.get('must_haves'))}"
    )
    questions = "\n".join(
        f"- ({q.get('kind')}) {q.get('q')}" for q in brief.get("likely_questions") or []
        if isinstance(q, dict)
    )
    sections.append(f"# Likely questions\n\n{questions}")
    sections.append(f"# Hidden notes (yours alone)\n\n{_lines(brief.get('hidden_notes'))}")
    if mode == "technical":
        tech = brief.get("technical") or {}
        problems = "\n\n".join(
            f"Problem {p.get('id')} — {p.get('title')} ({p.get('difficulty')}, {p.get('topic')})\n"
            f"{p.get('statement')}\nConstraints: {'; '.join(p.get('constraints') or [])}\n"
            f"Reference notes: {p.get('reference_notes')}\nFollow-up: {p.get('follow_up')}"
            for p in tech.get("problems") or []
            if isinstance(p, dict)
        )
        sections.append(f"# The coding problems\n\nFocus: {tech.get('focus')}\n\n{problems}")
    return "\n\n".join(sections)


def code_message(lang: str, code: str, *, submitted: bool) -> str:
    """The candidate's code as a user message."""
    head = CODE_SUBMITTED if submitted else (
        "[A snapshot of the candidate's editor while they work. Do not comment "
        "on it unless they asked you to look; it is here so you know where they are.]"
    )
    return f"{head}\n\n```{lang}\n{code}\n```"


# ── the debrief ──────────────────────────────────────────────────────────────

_DEBRIEF_SYSTEM = """\
You are an experienced interview coach writing a debrief after a practice \
interview. You have the brief the interviewer worked from and the full \
transcript. Be specific and kind, and be honest: quote or paraphrase the \
candidate's own words when you point at something, and say what a stronger \
answer would have contained. Scores are one to five, five being ready for \
the real thing. Fill every field; where a field does not apply (no case in \
a company interview, no code outside a technical one) write "n/a" for text \
and 0 for scores. next_practice is three concrete exercises for the next \
session. Write in plain prose; the page renders your text, not markdown.\
"""


def debrief_request(
    mode: str,
    brief: dict[str, Any],
    transcript: list[dict[str, Any]],
    code: list[dict[str, Any]],
) -> tuple[str, str, dict[str, Any]]:
    lines = []
    for row in transcript:
        speaker = row.get("speaker", "")
        if speaker in ("interviewer", "candidate"):
            lines.append(f"[{int(row.get('t', 0)) // 60:02d}:{int(row.get('t', 0)) % 60:02d}] "
                         f"{speaker}: {row.get('text', '')}")
    submitted = [c for c in code if c.get("event") == "submit"]
    final_code = submitted[-1] if submitted else (code[-1] if code else None)
    parts = [
        f"Interview mode: {mode}.",
        f"Brief (JSON):\n{json.dumps(brief, ensure_ascii=False)}",
        "Transcript:\n" + ("\n".join(lines) or "(the candidate said nothing)"),
    ]
    if final_code:
        parts.append(
            f"Final code ({final_code.get('lang')}):\n```{final_code.get('lang')}\n"
            f"{final_code.get('code')}\n```"
        )
    if mode == "case":
        parts.append(
            "Compare the candidate's recommendation with the historical outcome "
            "in the brief, and say where their reasoning matched or diverged."
        )
    parts.append("Write the debrief as JSON matching the schema.")
    return _DEBRIEF_SYSTEM, "\n\n".join(parts), DEBRIEF_SCHEMA


def render_debrief(debrief: dict[str, Any], mode: str) -> str:
    """The readable version, written next to the JSON."""
    out = [f"# Debrief\n", f"**Overall ({debrief.get('score_1_5', '?')}/5).** {debrief.get('overall', '')}\n"]

    def block(title: str, items: Any) -> None:
        if isinstance(items, list) and items:
            out.append(f"## {title}\n")
            out.extend(f"- {i}" for i in items)
            out.append("")

    block("Strengths", debrief.get("strengths"))
    block("What to work on", debrief.get("improvements"))
    per_q = debrief.get("per_question")
    if isinstance(per_q, list) and per_q:
        out.append("## Question by question\n")
        for item in per_q:
            if isinstance(item, dict):
                out.append(f"- **{item.get('q', '')}** ({item.get('score_1_5', '?')}/5) — {item.get('note', '')}")
        out.append("")
    comm = debrief.get("communication")
    if isinstance(comm, dict):
        out.append("## Communication\n")
        for key in ("clarity", "structure", "pace"):
            out.append(f"- **{key.title()}.** {comm.get(key, '')}")
        out.append("")
    if mode == "case" and isinstance(debrief.get("case"), dict):
        case = debrief["case"]
        out.append("## The case\n")
        out.append(f"- **Your recommendation.** {case.get('recommendation_given', '')}")
        out.append(f"- **Against what really happened.** {case.get('vs_historical_outcome', '')}")
        out.append(f"- **Structure.** {case.get('structure_score_1_5', '?')}/5\n")
    if mode == "technical" and isinstance(debrief.get("code"), dict):
        code = debrief["code"]
        out.append(f"## The code ({code.get('score_1_5', '?')}/5)\n")
        for key in ("correctness", "complexity", "style"):
            out.append(f"- **{key.title()}.** {code.get(key, '')}")
        out.append("")
    block("Practise next", debrief.get("next_practice"))
    return "\n".join(out).rstrip() + "\n"


# ── scripted answers, for the dev server and the probes ──────────────────────

_LEDGERLINE = {
    "company": {
        "name": "Ledgerline",
        "tagline": "Reconciliation and treasury tooling for mid-market finance teams",
        "industry": "Fintech (B2B SaaS)",
        "stage": "Series B",
        "size": "About one hundred and forty people",
        "hq": "Denver, with a Lisbon engineering office",
        "founded": "2019",
        "products": ["Ledgerline Reconcile", "Ledgerline Treasury", "an open banking connector layer"],
        "business_model": "Annual subscriptions tiered by transaction volume, plus a per-connector fee",
        "recent_news": [
            "Closed a forty-two million dollar Series B in the spring",
            "Launched Treasury, its second product, in the summer",
            "Named a new chief product officer from a payments company",
        ],
        "culture": "Small autonomous squads, written decision records, a strong QA habit born of one bad quarter",
    },
    "role": {
        "title": "Product Manager, Reconcile",
        "level": "Mid-level",
        "team": "the Reconcile squad (six engineers, a designer, and you)",
        "responsibilities": [
            "Own the matching-rules roadmap",
            "Run discovery with finance teams at customers",
            "Write the quarterly plan and the release notes",
        ],
        "must_haves": ["Has shipped a B2B product", "Comfortable with data and SQL", "Writes clearly"],
    },
    "interviewer": {"name": "Dana Okafor", "title": "Head of Product", "style": "Curious, direct, follows up on specifics"},
    "likely_questions": [
        {"q": "Tell me about yourself and why this role.", "kind": "behavioural"},
        {"q": "Walk me through a product you shipped end to end.", "kind": "role"},
        {"q": "How would you decide what the matching engine should do next?", "kind": "product"},
        {"q": "Tell me about a time you disagreed with an engineer.", "kind": "behavioural"},
        {"q": "How do you know a release went well?", "kind": "product"},
        {"q": "What questions do you have for me?", "kind": "role"},
    ],
    "hidden_notes": [
        "The Treasury launch slipped a quarter because Reconcile borrowed two engineers",
        "The biggest customer churned in the spring over unmatched transactions",
    ],
}

_PROBLEM = {
    "id": "p1",
    "title": "Merge transaction windows",
    "topic": "intervals and sorting",
    "difficulty": "medium",
    "statement": "You are given a list of transaction windows, each a pair of integers [start, end] "
                 "in minutes since midnight. Merge every set of overlapping or touching windows and "
                 "return the merged list sorted by start.",
    "examples": [
        {"input": "[[1,3],[2,6],[8,10],[15,18]]", "output": "[[1,6],[8,10],[15,18]]", "note": "[1,3] and [2,6] overlap"},
        {"input": "[[1,4],[4,5]]", "output": "[[1,5]]", "note": "touching windows merge"},
    ],
    "constraints": ["1 <= n <= 10^5", "0 <= start <= end <= 1440"],
    "starter": {
        "python": "def merge_windows(windows: list[list[int]]) -> list[list[int]]:\n    # windows: [[start, end], ...]\n    pass\n",
        "c": "// returns the number of merged windows written to out\nint merge_windows(int windows[][2], int n, int out[][2]) {\n    return 0;\n}\n",
        "cpp": "#include <vector>\nstd::vector<std::vector<int>> mergeWindows(std::vector<std::vector<int>>& windows) {\n    return {};\n}\n",
        "java": "import java.util.*;\nclass Solution {\n    public int[][] mergeWindows(int[][] windows) {\n        return new int[0][];\n    }\n}\n",
        "rust": "pub fn merge_windows(windows: &[[i32; 2]]) -> Vec<[i32; 2]> {\n    vec![]\n}\n",
        "javascript": "function mergeWindows(windows) {\n  // windows: [[start, end], ...]\n  return [];\n}\n",
        "typescript": "function mergeWindows(windows: number[][]): number[][] {\n  return [];\n}\n",
        "go": "func mergeWindows(windows [][]int) [][]int {\n\treturn nil\n}\n",
    },
    "reference_notes": "Sort by start, sweep once keeping the current window; O(n log n) time, O(n) space.",
    "follow_up": "What if windows arrive as a stream and you must answer merged-count queries at any time?",
}

_CASE = {
    "slug": "greenfield-grocer-market-entry",
    "title": "Greenfield Grocer: entering the next state",
    "type": "company",
    "client": "Greenfield Grocer, a regional grocery chain",
    "year": 2016,
    "prompt": "Our client runs sixty grocery stores across one state and is considering opening in the "
              "neighbouring state, where two national chains already operate. Should they enter, and how?",
    "background": "Greenfield is known for fresh produce and a loyalty programme. Margins are thin, "
                  "around two and a half percent net. The neighbouring state has three metro areas.",
    "clarifications": [
        {"q": "What is the client's objective?", "a": "Grow revenue by thirty percent in five years without losing margin."},
        {"q": "How do they currently grow?", "a": "Two or three new stores a year in their home state, which is nearly saturated."},
    ],
    "exhibits": [
        {"id": "e1", "title": "Neighbouring state: grocery market by metro", "kind": "table",
         "columns": ["Metro", "Population (k)", "Grocery spend ($M/yr)", "Chain A share", "Chain B share", "Independents"],
         "rows": [["Northfield", "900", "2,700", "35%", "30%", "35%"],
                  ["Riverton", "600", "1,800", "45%", "40%", "15%"],
                  ["Southbay", "400", "1,200", "20%", "15%", "65%"]],
         "note": "Shares are of spend.", "reveal_when": "The candidate asks about the market or its competitors."},
        {"id": "e2", "title": "New store economics", "kind": "table",
         "columns": ["Line", "Home state store", "Expected new-state store"],
         "rows": [["Revenue ($M/yr)", "18", "14"], ["Gross margin", "27%", "25%"],
                  ["Operating cost ($M/yr)", "4.4", "4.1"], ["Build cost ($M)", "9", "11"]],
         "note": "Expected figures are the client's estimates.", "reveal_when": "The candidate asks about store economics or payback."},
    ],
    "framework_hints": ["Market attractiveness, ability to win, economics", "Which metro first, and why", "Organic versus acquisition"],
    "good_answer": "Enter Southbay first, where independents hold sixty-five percent and no national chain is entrenched; "
                   "payback is roughly eleven to twelve years at the client's estimates, so the recommendation hinges on "
                   "lifting new-store revenue toward home-state levels; consider acquiring a small independent chain.",
    "historical_outcome": "The real chain entered the fragmented metro first via a small acquisition, reached "
                          "profitability in year three, and later expanded to the second metro; it never entered the "
                          "most concentrated one.",
    "sources": ["https://example.org/greenfield-case-notes"],
}


def scripted_json(schema: dict[str, Any], text: str) -> dict[str, Any]:
    """The canned answer for a schema, so the dev room needs no model."""
    title = schema.get("title")
    if title == "company_brief":
        return json.loads(json.dumps(_LEDGERLINE))
    if title == "technical_brief":
        brief = json.loads(json.dumps(_LEDGERLINE))
        brief["role"]["title"] = "Software Engineer, Reconcile"
        brief["interviewer"] = {"name": "Sam Reyes", "title": "Senior Engineer", "style": "Calm, asks about trade-offs"}
        brief["technical"] = {
            "focus": "algorithms and data structures",
            "what_to_expect": ["One medium problem on arrays or intervals", "Questions about complexity", "A follow-up variation"],
            "problems": [json.loads(json.dumps(_PROBLEM))],
        }
        return brief
    if title == "case":
        return json.loads(json.dumps(_CASE))
    if title == "debrief":
        return {
            "overall": "A solid practice run: clear examples, a little long on the opener.",
            "score_1_5": 3,
            "strengths": ["Concrete examples with numbers", "Asked good clarifying questions"],
            "improvements": ["Lead with the answer, then the story", "Tighten the opener to under a minute"],
            "per_question": [{"q": "Tell me about yourself", "note": "Ran long; the second half was the strong part.", "score_1_5": 3}],
            "communication": {"clarity": "Good", "structure": "Sometimes buried the point", "pace": "Even"},
            "case": {"recommendation_given": "n/a", "vs_historical_outcome": "n/a", "structure_score_1_5": 0},
            "code": {"correctness": "n/a", "complexity": "n/a", "style": "n/a", "score_1_5": 0},
            "next_practice": ["Answer three behavioural questions in under ninety seconds each",
                              "Practise the STAR shape out loud", "Prepare two questions for the interviewer"],
        }
    return {}


__all__ = [
    "BEGIN",
    "CASE_SCHEMA",
    "CODE_SUBMITTED",
    "COMPANY_BRIEF_SCHEMA",
    "CUTOFF_MARKER",
    "DEBRIEF_SCHEMA",
    "DIRECTIVE",
    "END",
    "LANGUAGES",
    "MODES",
    "TECHNICAL_BRIEF_SCHEMA",
    "brief_request",
    "brief_schema",
    "code_message",
    "debrief_request",
    "interviewer_prompt",
    "render_debrief",
    "scripted_json",
]
