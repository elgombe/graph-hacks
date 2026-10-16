"""The Analyst agent: Gemini function calling over the graph tool catalogue.

The model chooses tools and arguments; it never writes Cypher. Every call is traced
(tool, arguments, rows, time) and its evidence merged into one subgraph. The answer must
cite graph ids as [[id]]; a post-check flags any citation that is not in that evidence,
so the agent cannot name people, tickets or files it did not actually find.
"""

import re
import time
from dataclasses import dataclass, field
from datetime import date

from google.genai import errors, types

from . import config, tools
from .db import get_graph
from .evidence import Evidence
from .llm import client

MAX_STEPS = 6
CITE = re.compile(r"\[\[([^\]]+)\]\]")

SYSTEM = """You are Repo Brain, the memory of the {tenant} open-source organisation on GitHub.
You answer questions about its people, code, issues, PRs and decisions using ONLY the graph tools.

Rules:
- Always call tools before answering. If the question names a feature or concept rather than an
  exact file, ticket or person, call search first to find entry points.
- Pick the tool that fits the question: "why did we / what was decided" -> trace_decision;
  "who knows / who should review" -> who_knows; "what breaks / is anyone working on" -> impact;
  "how are X and Y related" -> connect; "single points of failure" -> bus_factor or key_people;
  "what are the modules" -> subsystems; details of one issue or PR -> ticket_context.
- Prefer a few precise tools over many broad ones, and stop calling tools once you can answer.
- Cite every person, ticket, file or directory you mention with its exact id from the tool results,
  in double brackets: [[@swilly22]], [[falkordb/falkordb-py#299]], [[falkordb/falkordb:src/execution_plan/]].
- Show the path behind the answer: say which PRs, reviews or links connect the things you name.
- If the tools find nothing relevant, say the graph does not contain it. Never guess or use outside knowledge.
- Be concise: a direct answer first, then the evidence as a few short bullet points.
Today is {today}. The graph covers about the last 12 months of GitHub activity."""


@dataclass
class Step:
    tool: str
    args: dict
    rows: int
    ms: int
    note: str = ""


@dataclass
class Answer:
    question: str
    text: str
    steps: list[Step] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    cited: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    outcome: str = "answered_with_path"  # | not_in_graph | no_tools_used | step_limit
    model: str = ""
    history: list[types.Content] = field(default_factory=list, repr=False)  # for follow-up questions


def _config(tenant: str, allow_tools: bool = True) -> types.GenerateContentConfig:
    decls = [types.FunctionDeclaration(name=d["name"], description=d["description"],
                                       parameters_json_schema=d["parameters"]) for d in tools.declarations()]
    mode = types.FunctionCallingConfigMode.AUTO if allow_tools else types.FunctionCallingConfigMode.NONE
    return types.GenerateContentConfig(
        system_instruction=SYSTEM.format(tenant=tenant, today=date.today().isoformat()),
        thinking_config=types.ThinkingConfig(thinking_level="low"),  # tool routing needs little deliberation
        tools=[types.Tool(function_declarations=decls)],
        tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode=mode)),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _generate(contents: list[types.Content], cfg: types.GenerateContentConfig,
              model: str | None) -> tuple[types.GenerateContentResponse, str]:
    """Call Gemini, falling back down AGENT_MODELS on overload. Once a conversation has a model
    it prefers to keep it (thought signatures are tied to it): it retries that model with backoff
    first and only then tries the others."""
    rounds = [config.AGENT_MODELS] * 3 if model is None else \
        [[model]] * 4 + [[m for m in config.AGENT_MODELS if m != model]]
    for attempt, names in enumerate(rounds):
        for name in names:
            try:
                return client().models.generate_content(model=name, contents=contents, config=cfg), name
            except errors.APIError as e:
                # 429/5xx: overloaded. 400 after a model switch: it rejected another model's history.
                if e.code not in (429, 500, 503) and not (e.code == 400 and name != model and model):
                    raise
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("Gemini is overloaded right now; try again in a minute")


def ask(question: str, tenant: str = "FalkorDB", history: list[types.Content] | None = None) -> Answer:
    g = get_graph(tenant)
    answer = Answer(question=question, text="")
    contents = [*(history or []), types.Content(role="user", parts=[types.Part.from_text(text=question)])]

    model = None
    for _ in range(MAX_STEPS):
        resp, model = _generate(contents, _config(tenant), model)
        content = resp.candidates[0].content
        contents.append(content)  # keeps the model's thought signatures for the next turn
        calls = [p.function_call for p in content.parts or [] if p.function_call]
        if not calls:
            answer.text = resp.text or ""
            break
        replies = []
        for call in calls:
            replies.append(_run_tool(g, call.name, dict(call.args or {}), answer))
        contents.append(types.Content(role="user", parts=replies))
    else:
        # Out of steps: answer from what was gathered, with tools switched off.
        answer.outcome = "step_limit"
        answer.text = _generate(contents, _config(tenant, allow_tools=False), model)[0].text or ""
    answer.model = model or ""

    _check_citations(answer)
    if not answer.steps:
        answer.outcome = "no_tools_used"
    elif not answer.evidence.nodes:
        answer.outcome = "not_in_graph"
    answer.history = contents
    return answer


def _run_tool(g, name: str, args: dict, answer: Answer) -> types.Part:
    started = time.perf_counter()
    try:
        result = tools.run(g, name, args)
        payload, rows, note = result.for_llm(), len(result.rows), result.note
        answer.evidence.merge(result.evidence)
    except Exception as e:  # a failing tool becomes a message to the model, not a crash
        payload, rows, note = {"error": str(e)[:300]}, 0, "error"
    answer.steps.append(Step(name, args, rows, int((time.perf_counter() - started) * 1000), note))
    return types.Part.from_function_response(name=name, response=payload)


def _check_citations(answer: Answer) -> None:
    """Keep [[id]] citations that resolve to evidence nodes; flag the rest."""
    known = answer.evidence.nodes

    def normalise(cid: str) -> str | None:
        for candidate in (cid, f"@{cid}", cid.lower(), cid.lower().rstrip("/") + "/"):
            if candidate in known:
                return candidate
        return None

    def replace(m: re.Match) -> str:
        cid = normalise(m.group(1).strip())
        if cid is None:
            answer.unverified.append(m.group(1))
            return f"{m.group(1)} (unverified)"
        if cid not in answer.cited:
            answer.cited.append(cid)
        return f"[[{cid}]]"

    answer.text = CITE.sub(replace, answer.text)
