"""A research/synthesis swarm whose only job is to produce interesting spend.

    planner -> 3 parallel researchers -> critic -> synthesizer

The shape is chosen for what it does to the token distribution, not for
answer quality. Each stage sees more accumulated context than the last, so
prompt tokens grow superlinearly down the graph while completion tokens stay
roughly flat. On the cost dashboard that shows up as the synthesizer dominating
spend while producing the fewest tokens -- the single most common and most
expensive surprise in real agent systems.

Attribution rides in per-node HTTP headers, so the proxy can price each node
separately without the graph knowing anything about billing.
"""
from __future__ import annotations

import json
import operator
import os
import uuid
from pathlib import Path
from typing import Annotated, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, START, StateGraph

PROXY_URL = os.environ.get("PROXY_URL", "http://127.0.0.1:8080/v1")
MODEL = os.environ.get("SWARM_MODEL", "spec-qwen3-8b")

# Each swarm node bills to its own Stripe test customer, so the invoice view
# reproduces the per-agent cost split independently of the local ledger --
# which is the only way to catch the two disagreeing.
_STATE = Path(__file__).resolve().parent.parent / "data" / "stripe.json"
if _STATE.exists():
    CUSTOMERS: dict[str, str] = json.loads(_STATE.read_text()).get("customers", {})
else:
    CUSTOMERS = {}
_FALLBACK_CUSTOMER = os.environ.get("STRIPE_CUSTOMER_ID", "")


def llm_for(agent: str, run_id: str, *, temperature: float = 0.4, max_tokens: int = 700):
    """One client per node. The headers are the entire billing integration."""
    return ChatOpenAI(
        base_url=PROXY_URL,
        api_key="not-needed-locally",
        model=MODEL,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=600,
        default_headers={
            "X-Agent": agent,
            "X-Run-Id": run_id,
            "X-Customer": CUSTOMERS.get(agent, _FALLBACK_CUSTOMER),
            "X-Request-Id": str(uuid.uuid4()),
        },
    )


class SwarmState(TypedDict):
    topic: str
    run_id: str
    angles: list[str]
    # Researchers run concurrently, so their writes must merge rather than
    # overwrite -- that is what the operator.add reducer is for.
    findings: Annotated[list[str], operator.add]
    critique: str
    report: str


N_RESEARCHERS = 3


def planner(state: SwarmState) -> dict:
    llm = llm_for("planner", state["run_id"], temperature=0.7, max_tokens=300)
    resp = llm.invoke([
        SystemMessage(content=(
            f"Break the user's topic into exactly {N_RESEARCHERS} distinct research "
            f"angles. Reply with {N_RESEARCHERS} lines, one angle per line, no "
            f"numbering, no preamble. /no_think"
        )),
        HumanMessage(content=state["topic"]),
    ])
    lines = [l.strip(" -*\t") for l in resp.content.splitlines() if l.strip()]
    angles = lines[:N_RESEARCHERS]
    while len(angles) < N_RESEARCHERS:
        angles.append(f"General background on {state['topic']}")
    return {"angles": angles}


def make_researcher(idx: int):
    def researcher(state: SwarmState) -> dict:
        angle = state["angles"][idx]
        llm = llm_for(f"researcher-{idx + 1}", state["run_id"], max_tokens=600)
        resp = llm.invoke([
            SystemMessage(content=(
                "You are a domain researcher. Write a dense, concrete briefing on "
                "the assigned angle: specific mechanisms, numbers and tradeoffs, no "
                "filler and no restating the question. /no_think"
            )),
            HumanMessage(content=f"Topic: {state['topic']}\nYour angle: {angle}"),
        ])
        return {"findings": [f"### {angle}\n{resp.content}"]}

    researcher.__name__ = f"researcher_{idx + 1}"
    return researcher


def critic(state: SwarmState) -> dict:
    # Sees all three briefings: prompt tokens jump here.
    llm = llm_for("critic", state["run_id"], temperature=0.2, max_tokens=500)
    joined = "\n\n".join(state["findings"])
    resp = llm.invoke([
        SystemMessage(content=(
            "You are a skeptical reviewer. Identify contradictions, unsupported "
            "claims and gaps across these briefings. Be specific and terse. /no_think"
        )),
        HumanMessage(content=joined),
    ])
    return {"critique": resp.content}


def synthesizer(state: SwarmState) -> dict:
    # Sees everything: briefings + critique. This node is the budget sink.
    llm = llm_for("synthesizer", state["run_id"], temperature=0.3, max_tokens=900)
    joined = "\n\n".join(state["findings"])
    resp = llm.invoke([
        SystemMessage(content=(
            "Synthesize the briefings into one coherent report, resolving the "
            "reviewer's objections explicitly. Use short sections. /no_think"
        )),
        HumanMessage(content=(
            f"Topic: {state['topic']}\n\n## Briefings\n{joined}\n\n"
            f"## Reviewer objections\n{state['critique']}"
        )),
    ])
    return {"report": resp.content}


def build_graph():
    g = StateGraph(SwarmState)
    g.add_node("planner", planner)
    for i in range(N_RESEARCHERS):
        g.add_node(f"researcher_{i + 1}", make_researcher(i))
    g.add_node("critic", critic)
    g.add_node("synthesizer", synthesizer)

    g.add_edge(START, "planner")
    # Fan out: LangGraph runs these concurrently, which is also what makes the
    # proxy's per-agent metrics worth having -- they interleave on the wire.
    for i in range(N_RESEARCHERS):
        g.add_edge("planner", f"researcher_{i + 1}")
        g.add_edge(f"researcher_{i + 1}", "critic")
    g.add_edge("critic", "synthesizer")
    g.add_edge("synthesizer", END)
    return g.compile()
