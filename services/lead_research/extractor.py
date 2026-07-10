"""Turn raw context.dev material into four layers, then run the depth guard.

Two separate LLM calls, on purpose:

  extract_layers()   raw markdown/snippets -> {quote, derived_operational,
                     behavioural, live_move}
  run_depth_guard()  layers + the student's work-principle -> a synthesis line,
                     plus a structured verdict on whether it is too shallow.

Keeping them apart means a change to the scraper cannot silently corrupt the
layer semantics, and the guard's verdict is auditable on its own.

The layers are not equal. Priority is fixed, and the caller walks DOWN it until a
line survives the depth guard's swap test:

  1. quote                their own words. A post, a talk. The strongest fuel,
                          because it is the person talking rather than a database
                          describing them.
  2. derived_operational  not the title, what the title REQUIRED. Reconstructed
                          from a career table: concrete nouns, durations, domain
                          switches.
  3. behavioural          the unguarded signal. Nothing to do with their role,
                          everything to do with how they think. Often the reason
                          a stranger replies.
  4. reflected_skills     the last resort before silence. When nothing above
                          exists, reason about what someone doing THAT function at
                          THAT kind of company would have learned. It must be
                          anchored to the specific combination (function + company
                          type + any stated tenure), never to the role in the
                          abstract: "vendor contracts at a footwear brand" survives
                          the swap; "as a compliance specialist you value accuracy"
                          does not. Infer skills, never invent facts — every noun
                          must trace to something the research actually states.
  5. live_move            a store opening, a fundraise. Texture only. NEVER the
                          load-bearing line.

An About section is a tagline. It is never the synthesis line.
"""

from __future__ import annotations

from core.config import settings
from core.logger import get_logger
from services.shared.ai.azure_openai_client import generate_json

logger = get_logger(__name__)

_MAX_CHARS = 24_000  # cap the material we hand the model

LAYER_PRIORITY = ("quote", "derived_operational", "behavioural",
                  "reflected_skills", "live_move")

# Layers that can carry a synthesis line. `live_move` is texture and never can.
CARRIER_LAYERS = ("quote", "derived_operational", "behavioural", "reflected_skills")


_EXTRACT_SYSTEM = (
    "You extract verifiable facts about ONE named person from scraped web text. "
    "You never invent, infer beyond the evidence, or fill a field to be helpful. "
    "An empty field is correct and expected. If the text is about a different "
    "person with the same name, return all nulls."
)

_EXTRACT_PROMPT = """Extract what is actually established about {name}{at_company} from the material below.

Return four fields. Any may be null. Do not stretch to fill one.

quote: something {name} personally wrote or said, quoted verbatim, expressing a VIEW they
hold about their work or their field. A post, a public reply, a talk.
This is null unless ALL of the following hold:
  - the words are demonstrably theirs, not a journalist's or a colleague's;
  - they say something only THIS person would say, not something any employee of the
    company could say. A testimonial on a careers page, a work-anniversary post, or
    praise for a company perk ("our Fun Friday lifts morale") is null: every colleague
    could have written it.
  - it is not a job description, a company tagline, an About section, or text a
    database wrote about them.
If you are unsure whether the words are theirs, return null. Null is the correct and
expected answer for most people.

quote_source_url: the URL the quote came from, if identifiable.

derived_operational: what this person's career REQUIRED them to actually do. Not their
title, and NOT their title reworded. Reconstruct from the trajectory: what did they
move, build, run, or own, for how long, and across which domains? Concrete nouns,
durations, and domain switches.
Good: "twenty years moving physical goods, semiconductors then industrial gas cylinders,
before carrying that into a D2C footwear supply chain"
Bad: "experienced operations leader with a track record of excellence"
Bad: "compliance management, ensuring legal compliance and contracts" (this is the job
title restated; it would be true of anyone holding that title)
If the only thing you can say is a paraphrase of their current title, return null.

behavioural: something true about how this person thinks or what they have been through,
which has little to do with their job. A stated opinion, a setback, an unguarded take.
Not a skill. Not an achievement award unless the award reveals something about them.

reflected_skills: THE LAST RESORT, used only when the three fields above are all null.
Not what they are called. What someone doing this FUNCTION at this KIND of company would
have actually learned to handle. Reason from the specific combination, never the abstract
role.

  Anchor it to the concrete: the function, the company and what that company sells, and
  any tenure the research explicitly states. Then name the recurring problem that
  combination creates.

  Good: "vendor contracts at a footwear brand, where every new supplier is another
  agreement to paper before a launch can ship"
  Good: "logistics for a company moving physical goods to customers, where a delayed
  shipment is a refund"
  Bad: "as a compliance specialist you value accurate contracts"  (the role in the
  abstract; true of every compliance specialist anywhere)
  Bad: "four years of experience"  (unless a snippet states the tenure)

  INFER SKILLS, NEVER INVENT FACTS. Every noun must trace to something stated in the
  MATERIAL. Do not state tenure, headcount, funding, metrics, growth, or events that
  the material does not contain. If you cannot anchor it to a stated fact, return null.

live_move: a recent company event (fundraise, launch, store opening) in one clause.
Texture only.

MATERIAL:
{material}"""

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "quote": {"type": ["string", "null"]},
        "quote_source_url": {"type": ["string", "null"]},
        "derived_operational": {"type": ["string", "null"]},
        "behavioural": {"type": ["string", "null"]},
        "reflected_skills": {"type": ["string", "null"]},
        "live_move": {"type": ["string", "null"]},
    },
    "required": ["quote", "derived_operational", "behavioural",
                 "reflected_skills", "live_move"],
}


def _clip(parts: list[str], budget: int) -> str:
    out, used = [], 0
    for p in parts:
        if not p:
            continue
        take = p[: max(0, budget - used)]
        if not take:
            break
        out.append(take)
        used += len(take)
    return "\n\n---\n\n".join(out)


def extract_layers(bundle, name: str, company: str = "") -> dict:
    """Distil a ResearchBundle into the four layers. Returns a dict of str|None."""
    if not bundle.has_signal():
        return {k: None for k in (*LAYER_PRIORITY, "quote_source_url")}

    material = _clip(
        [
            *(f"[LinkedIn snippet] {s}" for s in bundle.linkedin_snippets),
            *(f"[X] {m}" for m in bundle.x_markdown),
            *(f"[Web] {m}" for m in bundle.web_markdown),
        ],
        _MAX_CHARS,
    )

    prompt = _EXTRACT_PROMPT.format(
        name=name,
        at_company=f" at {company}" if company else "",
        material=material,
    )
    result = generate_json(
        prompt, _EXTRACT_SCHEMA, temperature=0.2,
        system_prompt=_EXTRACT_SYSTEM,
        deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT,
    )

    layers = {k: (result.get(k) or None) for k in (*LAYER_PRIORITY, "quote_source_url")}
    logger.info("[Research] Layers for %s: %s", name,
                {k: bool(v) for k, v in layers.items()})
    return layers


# ── Depth guard ────────────────────────────────────────────────────────────────

_GUARD_SYSTEM = (
    "You write one sentence, then you judge it honestly, and you are rewarded for "
    "killing your own sentence. A shallow line is worse than no line at all."
)

_GUARD_PROMPT = """Write ONE synthesis sentence for a cold email, then judge it.

THE RECIPIENT: {name}, {title} at {company}

WHAT WE KNOW ABOUT THEM (use the HIGHEST-priority non-empty field; walk down only
when the one above it is empty):
1. THEIR OWN WORDS: {quote}
2. WHAT THEIR CAREER REQUIRED: {derived_operational}
3. HOW THEY THINK: {behavioural}
4. WHAT THEIR WORK WOULD HAVE TAUGHT THEM (last resort): {reflected_skills}
5. LIVE MOVE (texture only, never the basis): {live_move}

THE SENDER'S WORK-PRINCIPLE (the one choice or trick that made their own project work):
{work_principle}

THE SENDER'S PROJECT:
{sender_project}

Write a sentence that names the recipient's specific thing, then joins it to ONE true,
SMALLER claim about the sender. The sender's claim must be the smaller of the two. Saying
so plainly is what makes it land.

Return TWO fields:
  synthesis_line   the full sentence, both halves.
  recipient_clause ONLY the half about the recipient, with the sender's half stripped
                   out entirely. It must contain nothing the sender did. This is the
                   only part the email writer will see attributed to the recipient.

Example:
  synthesis_line   = "Papering a vendor agreement for every supplier before a footwear
                      drop can ship taught you to spot risk early, and batching reviews
                      by clause type taught me to spot patterns."
  recipient_clause = "Papering a vendor agreement for every supplier before a footwear
                      drop can ship taught you to spot risk early."

FORBIDDEN:
- Equivalence: "same bet", "exactly what I did", "we both", "just like you"
- Hedges: "feels relevant", "seems similar", "might align", "resonates with"
- Praise of the person or the company
- Restating their point back to them without adding the sender's own claim
- Building the line on the LIVE MOVE

THEN APPLY THE SWAP TEST, mechanically:
Replace {name} with a DIFFERENT person holding the same title at the same company.
Read your sentence again as if addressed to that other person.
Does it still read as true?

Swap only the RECIPIENT. The sender's half of the sentence never changes, and can never
make a line pass: "and I batched contract reviews by clause type" is true regardless of
who receives it. Judge ONLY the clause about the recipient.

  survives_swap = true   -> the sentence is about the ROLE or the COMPANY, not the
                            person. Too shallow. This is the correct answer for most
                            lines, and the answer you should default to.
  survives_swap = false  -> the sentence is true only of this specific human. It ships.

A sentence naming a specific THING is not the same as a sentence about a specific
PERSON. Ask: is this thing unique to them, or shared by everyone at the company?

Examples:
  "Twenty years moving industrial gas and semiconductors before D2C footwear"
      -> false. Only Sreedhar could be described this way. It ships.
  "As Head of Logistics you care about cost-per-shipment"
      -> true. Anyone in that chair. It dies.
  "Your Fun Friday activity uplifts employee morale"
      -> TRUE. Fun Friday is a company-wide perk; every colleague shares it. Naming it
         feels specific but is not. It dies.
  "Congrats on the Series A"
      -> true. Shared by the whole company. It dies.
  "Your work anniversary post about growth at the company"
      -> true. Anyone can post that. It dies.

Anything drawn from a careers page, a testimonial, a company perk, a fundraise, or a
work-anniversary post SURVIVES the swap. Answer true for all of them.

IF YOU ARE USING LAYER 4 (what their work would have taught them), the swap test is
harsher, not softer. A sentence about the ROLE dies. A sentence about the specific
COMBINATION of function + what this company actually sells can live, because that
combination is not shared by everyone with the same title elsewhere.

  "As a compliance specialist you value accurate contracts"
      -> true. Every compliance specialist anywhere. It dies.
  "Papering a vendor agreement for every new supplier before a footwear drop can ship"
      -> false. That is compliance AT A SHOE COMPANY, not compliance in general.
         Ships.

Never state a fact the research did not give you: no tenure, no headcount, no funding,
no metrics, no growth. Infer what the work teaches; never invent what happened.

Be strict. If you are unsure, answer true and let the line die."""

_GUARD_SCHEMA = {
    "type": "object",
    "properties": {
        "synthesis_line": {"type": "string"},
        # The recipient's clause ALONE. The email writer must never be handed the
        # fused line: it renders `belief` as "WHAT THEY APPEAR TO BELIEVE", so a
        # fused line makes the sender's own technique read as the recipient's, and
        # the email compliments them on the sender's idea.
        "recipient_clause": {"type": "string"},
        "survives_swap": {"type": "boolean"},
        "layer": {"type": "string", "enum": list(CARRIER_LAYERS)},
        "reason": {"type": "string"},
    },
    "required": ["synthesis_line", "recipient_clause", "survives_swap", "layer", "reason"],
}


def run_depth_guard(layers: dict, name: str, title: str, company: str,
                    work_principle: str, sender_project: str) -> dict:
    """Propose a synthesis line and judge it.

    Returns {synthesis_line, survives_swap, layer, reason}. `survives_swap=True`
    means REJECTED — the caller must drop to the bare ask.
    """
    usable = [k for k in CARRIER_LAYERS if layers.get(k)]
    if not usable:
        # Only a live move, or nothing. Live moves are texture and cannot carry a line.
        return {
            "synthesis_line": None,
            "recipient_clause": None,
            "survives_swap": True,
            "layer": "live_move" if layers.get("live_move") else "none",
            "reason": "No carrier layer; only a live move or nothing. Bare ask.",
        }

    prompt = _GUARD_PROMPT.format(
        name=name, title=title or "", company=company or "",
        quote=layers.get("quote") or "(none)",
        derived_operational=layers.get("derived_operational") or "(none)",
        behavioural=layers.get("behavioural") or "(none)",
        reflected_skills=layers.get("reflected_skills") or "(none)",
        live_move=layers.get("live_move") or "(none)",
        work_principle=work_principle or "(not provided)",
        sender_project=sender_project or "(not provided)",
    )
    result = generate_json(
        prompt, _GUARD_SCHEMA, temperature=0.4,
        system_prompt=_GUARD_SYSTEM,
        deployment=settings.AZURE_OPENAI_EMAIL_DEPLOYMENT,
    )

    verdict = {
        "synthesis_line": (result.get("synthesis_line") or "").strip() or None,
        "recipient_clause": (result.get("recipient_clause") or "").strip() or None,
        # Absent/garbled verdict must fail closed: reject, don't ship.
        "survives_swap": bool(result.get("survives_swap", True)),
        "layer": result.get("layer") or "none",
        "reason": (result.get("reason") or "").strip(),
    }
    # Fail closed: without a clean recipient-only clause we cannot safely attribute
    # anything, so drop to the bare ask rather than risk crediting the recipient
    # with the sender's work.
    if not verdict["synthesis_line"] or not verdict["recipient_clause"]:
        verdict["survives_swap"] = True

    logger.info("[DepthGuard] %s: layer=%s survives_swap=%s (%s)",
                name, verdict["layer"], verdict["survives_swap"],
                verdict["reason"][:80])
    return verdict
