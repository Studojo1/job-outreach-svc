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

reflected_skills: ALWAYS produce this when you know their function and their company.
It is the fallback that makes almost every lead writable, and it must be null only when
you genuinely do not know what they do.

Not what they are called. What someone doing this FUNCTION at this KIND of company would
have actually learned to handle. You have the title and the company; that is enough. Name
the recurring problem that combination creates.

  Anchor it to the concrete: the function, and what that company actually sells or does.
  Then state the problem that pairing produces day to day.

  Good: "vendor contracts at a footwear brand, where every new supplier is another
  agreement to paper before a launch can ship"
  Good: "logistics for a company moving physical goods to customers, where a delayed
  shipment is a refund"
  Good: "strategy at a workforce-management company, where every product bet has to
  survive contact with how real shift schedules actually behave"
  Bad: "as a compliance specialist you value accurate contracts"  (the role in the
  abstract, with no company in it)
  Bad: "four years of experience"  (unless a snippet states the tenure)

  INFER SKILLS, NEVER INVENT FACTS. Do not state tenure, headcount, funding, metrics, or
  events the material does not contain. But the title and the company ARE stated facts:
  reason from them freely. Returning null here because you are being cautious is the
  wrong call. Return null ONLY if you do not know their function or their company.

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

THE SENDER IS LOOKING FOR:
{sender_target_role}

WHAT THE COMPANY IS DOING (bridge material only, NEVER the basis of the line):
{signals_block}

Write the line in up to THREE parts, in this order:

  1. RECIPIENT (always)  the specific thing they do or said. This opens the sentence.
  2. BRIDGE (only if it earns its place)  what the company is doing, stated as the
     reason the recipient's problem is growing right now. One subordinate clause.
     Include it ONLY when it makes the recipient's work and the sender's work collide.
     Omit it entirely otherwise. It never opens the sentence and never stands alone.
  3. SENDER (always)  ONE true, SMALLER claim about the sender. Smaller than the
     recipient's. Saying so plainly is what makes it land.

Return FOUR fields:
  synthesis_line   the full sentence, all parts you used.
  recipient_clause ONLY part 1, with the bridge AND the sender's half stripped out. It
                   must contain nothing the sender did and no company fact. This is the
                   only text the email writer will attribute to the recipient.
  bridge_clause    ONLY part 2, the company-signal clause, or "" if you omitted it. It
                   is a fact about the COMPANY, never about the recipient personally.
  bridge_used      true if you included part 2, false if you omitted it.
  survives_swap    see the swap test below.

Example WITH a bridge (the company signal earns its place):
  synthesis_line   = "Papering a vendor agreement for every new supplier taught you to
                      spot risk early, and twelve new stores means more contracts, not
                      fewer. I built a review tracker that cut turnaround from five days
                      to one."
  recipient_clause = "Papering a vendor agreement for every new supplier taught you to
                      spot risk early."
  bridge_used      = true

Example WITHOUT a bridge (no signal, or the signal connects to nothing):
  synthesis_line   = "Papering a vendor agreement for every supplier before a footwear
                      drop can ship taught you to spot risk early, and batching reviews
                      by clause type taught me to spot patterns."
  recipient_clause = "Papering a vendor agreement for every supplier before a footwear
                      drop can ship taught you to spot risk early."
  bridge_used      = false

FORBIDDEN:
- Equivalence: "same bet", "exactly what I did", "we both", "just like you"
- Hedges: "feels relevant", "seems similar", "might align", "resonates with"
- Praise of the person or the company
- Restating their point back to them without adding the sender's own claim
- Building the line on the LIVE MOVE or on any COMPANY SIGNAL

THEN APPLY THE SWAP TEST, mechanically:
Replace {name} with a DIFFERENT person holding the same title at the same company.
Read your sentence again as if addressed to that other person.
Does it still read as true?

Swap only the RECIPIENT. The sender's half of the sentence never changes, and can never
make a line pass: "and I batched contract reviews by clause type" is true regardless of
who receives it. Judge ONLY the clause about the recipient.

Strip the company signal before you judge. A hiring notice, a fundraise, a store opening
or a product launch is true of every employee, so it can never make a line survive. If
removing the company clause leaves nothing person-specific, answer true and let it die.

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
work-anniversary post SURVIVES the swap when it is the BASIS of the line. Answer true
for all of them. (A company fact used as a bridge in part 2 is fine, because part 1
already carries the line on its own.)

IF YOU ARE USING LAYER 4 (what their work would have taught them), read the swap test
carefully, because layer 4 is BY DEFINITION derived from their function and their
company. Judged naively, it would always die, and that is wrong.

The swap is: a DIFFERENT PERSON, same title, SAME COMPANY. It is NOT: the same title at
a different company. So a line naming what this company actually does is doing real
work. It would be false of someone with that title anywhere else, and that is what makes
it worth sending.

  "As a compliance specialist you value accurate contracts"
      -> survives. No company in it. True of every compliance specialist alive. Dies.
  "Papering a vendor agreement for every new supplier before a footwear drop can ship"
      -> does not survive. That is compliance AT A SHOE COMPANY, not compliance in
         general. SHIPS.
  "Strategy at a workforce-management company, where every product bet has to survive
   contact with how real shift schedules behave"
      -> does not survive. Strategy at a bank looks nothing like this. SHIPS.

Do NOT reject a layer-4 line merely because a colleague in the same seat would recognise
it. Of course they would; they do the same job at the same company. The question is
whether the sentence would still be true if you moved that person to a DIFFERENT company
with the same title. If it would not, the line is doing its job.

Never state a fact the research did not give you: no tenure, no headcount, no funding,
no metrics, no growth. Infer what the work teaches; never invent what happened.

Be strict. If you are unsure, answer true and let the line die.

DECIDING ON THE BRIDGE (part 2).

In `bridge_reasoning`, go through the company signals ONE AT A TIME. For each one,
write a single line in exactly this form:

    <signal> -> raises the recipient's workload, OR shows the company already
                acknowledges the problem the sender solves? yes/no, because ...
    <signal> -> does the sender's work address it? yes/no

A signal qualifies on EITHER count:
  (a) it makes the recipient's problem bigger  (twelve new stores -> more suppliers ->
      more agreements to paper), OR
  (b) it is an OPEN ROLE that matches THE SENDER IS LOOKING FOR. A matching open role
      means the company has already admitted it needs exactly what the sender has
      built. That is the strongest bridge available: it is not a generic company fact,
      it is the collision between this recipient's workload and this sender's tooling.
      Compare the open role to the sender's target role by MEANING, not exact wording
      ("Legal Ops Associate" matches "legal ops analyst").

Do this for every signal listed under WHAT THE COMPANY IS DOING. Do not summarise, do
not skip any, and do not decide before you have written them all out.

Then: if ANY signal got yes to both questions, set bridge_used = true and weave that one
signal into part 2 as a subordinate clause. Otherwise set bridge_used = false.

A worked example of the reasoning:
    "opened 12 new retail stores" -> bigger? yes, more stores means more suppliers,
      which means more agreements to paper.
    "opened 12 new retail stores" -> sender addresses it? yes, a review tracker that
      cuts turnaround from five days to one.
    -> bridge_used = true
    -> "...taught you to spot risk early, and twelve new stores means more contracts,
        not fewer. I built a review tracker that cut turnaround from five days to one."

    "raised a Series B" -> bigger? no, funding does not add contracts to paper.
    "hiring a Legal Ops Associate" (sender wants: legal ops analyst) -> qualifies on
      (b): the role matches, so the company has already acknowledged the problem the
      sender solves. -> sender addresses it? yes.
      -> bridge_used = true
      -> "...taught you to spot risk early, and with legal ops being staffed up that is
          more agreements through one pipeline. I built a review tracker that cut
          turnaround from five days to one."
      NOTE the phrasing. NEVER write "I saw you are hiring a Legal Ops Associate" or
      "I am applying for the Legal Ops Associate role". Reciting the job posting is a
      job application, not a bridge, and the email already ends with the ask. The role
      may only appear as the REASON the recipient's workload is changing.

    "hiring a Warehouse Supervisor" (sender wants: legal ops analyst) -> qualifies on
      (b)? no, the role does not match. On (a)? no. -> omit.

The bridge is a subordinate clause. It never opens the sentence, never stands alone, and
the recipient's clause must still read true with the bridge deleted.
"""

_GUARD_SCHEMA = {
    "type": "object",
    "properties": {
        "synthesis_line": {"type": "string"},
        # The recipient's clause ALONE. The email writer must never be handed the
        # fused line: it renders `belief` as "WHAT THEY APPEAR TO BELIEVE", so a
        # fused line makes the sender's own technique read as the recipient's, and
        # the email compliments them on the sender's idea.
        "recipient_clause": {"type": "string"},
        # Forcing the model to WRITE this reasoning is what makes it use the bridge at
        # all. Without the field it defaults to omitting the signal every time, even
        # when it later agrees the signal was relevant. Measured on Divakar Sharma.
        "bridge_reasoning": {"type": "string"},
        # Auditable: did the company signal actually earn a place, or was it ignored?
        "bridge_used": {"type": "boolean"},
        "bridge_clause": {"type": "string"},
        "survives_swap": {"type": "boolean"},
        "layer": {"type": "string", "enum": list(CARRIER_LAYERS)},
        "reason": {"type": "string"},
    },
    "required": ["synthesis_line", "recipient_clause", "bridge_reasoning",
                 "bridge_used", "bridge_clause", "survives_swap", "layer", "reason"],
}


def run_depth_guard(layers: dict, name: str, title: str, company: str,
                    work_principle: str, sender_project: str,
                    company_signals: dict | None = None,
                    sender_target_role: str = "") -> dict:
    """Propose a synthesis line and judge it.

    `company_signals` (hiring, momentum, what they build, open roles) come from the
    globally-cached CompanyProfile. They are BRIDGE material, never the basis of the
    line: CompanyProfile is keyed on `domain`, so every fact in it is identical for
    every lead at that company and therefore survives the swap test by construction.
    A line built on one is the old system's "I noticed Neeman's has been expanding".

    They earn a place only when they explain WHY NOW — why the recipient's specific
    thing and the sender's specific thing collide at this moment.

    Returns {synthesis_line, recipient_clause, survives_swap, layer, reason}.
    `survives_swap=True` means REJECTED — the caller drops to the bare ask.
    """
    usable = [k for k in CARRIER_LAYERS if layers.get(k)]
    if not usable:
        # Only a live move, or nothing. Live moves are texture and cannot carry a line.
        return {
            "synthesis_line": None,
            "recipient_clause": None,
            "bridge_used": False,
            "bridge_clause": None,
            "survives_swap": True,
            "layer": "live_move" if layers.get("live_move") else "none",
            "reason": "No carrier layer; only a live move or nothing. Bare ask.",
        }

    sig = company_signals or {}
    sig_lines = []
    if sig.get("hiring_signal"):
        sig_lines.append(f"HIRING NOW: {sig['hiring_signal']}")
    if sig.get("open_roles"):
        sig_lines.append(f"OPEN ROLES: {', '.join(sig['open_roles'][:3])}")
    if sig.get("recent_momentum"):
        sig_lines.append(f"RECENT MOVE: {sig['recent_momentum']}")
    if sig.get("what_they_build"):
        sig_lines.append(f"WHAT THE COMPANY BUILDS: {sig['what_they_build']}")
    signals_block = ("\n".join(sig_lines)) if sig_lines else "(none known)"

    prompt = _GUARD_PROMPT.format(
        signals_block=signals_block,
        sender_target_role=sender_target_role or "(not stated)",
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
        "bridge_used": bool(result.get("bridge_used", False)),
        "bridge_clause": (result.get("bridge_clause") or "").strip() or None,
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

    logger.info("[DepthGuard] %s: layer=%s bridge=%s survives_swap=%s (%s)",
                name, verdict["layer"], verdict["bridge_used"],
                verdict["survives_swap"], verdict["reason"][:70])
    return verdict
