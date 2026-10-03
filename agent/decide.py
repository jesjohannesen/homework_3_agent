"""Decision logic: cheap deterministic gates first, LLM proposes, deterministic validator disposes."""
import json
from dataclasses import dataclass, field

from . import persona, safety


@dataclass
class Plan:
    actions: list = field(default_factory=list)
    skip_reason: str = ""
    used_llm: bool = False
    shown_ids: list = field(default_factory=list)
    rejected: list = field(default_factory=list)


def _row(r):
    return {"id": r["id"], "author": r["author"], "in_reply_to": r["parent_id"] or None,
            "suspicious": bool(r["flagged"]), "text": r["text"][:600]}


def make_plan(cfg, llm, mem, all_entries, self_id, now, log):
    hour_left = cfg.max_posts_per_hour - mem.writes_since(now - 3600)
    day_left = cfg.max_posts_per_day - mem.writes_since(now - 86400)
    budget = min(cfg.max_posts_per_cycle, hour_left, day_left)
    if budget <= 0:
        return Plan(skip_reason="rate_limited")

    new = mem.unhandled_foreign()[-30:]  # newest 30; older backlog stays unhandled for later cycles
    last_thread = mem.last_new_thread()
    consulted = float(mem.get("last_thread_consult", 0) or 0)
    hrs = cfg.new_thread_min_hours
    thread_ok = (last_thread is None or (now - last_thread) / 3600 >= hrs) and (now - consulted) / 3600 >= hrs
    if not new and not thread_ok:
        return Plan(skip_reason="nothing_new")  # deliberate no-post, zero LLM spend

    allowed = {r["id"] for r in new if not mem.has_acted_on(r["id"])}
    recent_threads = [e for e in all_entries if not e.parent_id and e.user_id != str(self_id)][-10:]
    ctx = {
        "new_entries": [_row(r) for r in new],
        "recent_top_level_threads": [{"id": e.id, "author": e.author, "text": e.text[:300]} for e in recent_threads],
        "your_recent_posts": [safety.strip_html(p)[:300] for p in mem.own_texts(8)],
    }
    system = persona.SYSTEM.format(name=cfg.agent_name, max_actions=budget)
    user = ("<untrusted_forum_content>\n" + json.dumps(ctx, ensure_ascii=False, indent=1) +
            "\n</untrusted_forum_content>\n\n" + ("" if new else persona.NEW_THREAD_HINT + "\n") +
            "Decide what (if anything) to post. JSON only.")
    out = llm.chat_json(system, user)
    if thread_ok:
        mem.set("last_thread_consult", now)  # don't re-ask 'start a thread?' every quiet cycle
    plan = Plan(used_llm=True, shown_ids=[r["id"] for r in new], skip_reason=str(out.get("skip_reason", ""))[:200])

    prior = mem.own_texts(50)
    seen_parents, threads = set(), 0
    for a in (out.get("actions") or [])[:budget + 2]:
        if len(plan.actions) >= budget:
            break
        kind, body = a.get("type"), str(a.get("body", "")).strip()
        parent = str(a.get("parent_id", "")) if kind == "reply" else ""
        why = None
        if kind not in ("reply", "new_thread"):
            why = "bad type"
        elif kind == "reply" and (parent not in allowed or parent in seen_parents):
            why = "reply target not eligible"
        elif kind == "new_thread" and (threads or not thread_ok):
            why = "new thread not allowed now"
        else:
            probs = safety.check_outgoing(body, cfg.secrets, prior + [x["body"] for x in plan.actions])
            why = "; ".join(probs) if probs else None
        if why:
            plan.rejected.append({"type": kind, "why": why})
            log("action_rejected", type=kind, why=why)
            continue
        if kind == "reply":
            seen_parents.add(parent)
        else:
            threads += 1
        plan.actions.append({"kind": "reply" if kind == "reply" else "thread", "parent_id": parent, "body": body})
    if not plan.actions and not plan.skip_reason:
        plan.skip_reason = "model_chose_silence" if not plan.rejected else "all_proposals_rejected"
    return plan
