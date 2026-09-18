"""Psychometric question bank — 8 questions, each probing 4 dimensions.

Every option carries dimensional weights. The adaptive engine picks the
best 5 from this pool based on existing signals from onboarding answers
and resume data.

Question design principles:
- Each question has exactly 4 options mapping cleanly to the 4 dimensions
- Some options carry cross-dimension weights (e.g., analytical + execution)
  to avoid forcing binary choices on nuanced people
- Questions are scenario-based, not self-report ("what do you do" > "what are you")
- Written in second person, conversational tone matching Studojo voice
"""

from __future__ import annotations

PSYCHOMETRIC_QUESTIONS: list[dict] = [
    {
        "id": "psych_decision",
        "key": "psych_decision",
        "text": "When you're making an important call with incomplete info, you usually:",
        "options": [
            {"label": "A", "text": "Gather more data before deciding", "weights": {"analytical": 2.0, "strategic": 0.5}},
            {"label": "B", "text": "Trust your gut and move fast", "weights": {"creative": 1.5, "execution": 0.5}},
            {"label": "C", "text": "Go with what's worked before", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Ask someone with more context", "weights": {"social": 1.5, "communication": 0.5}},
        ],
        "primary_dimensions": ["analytical", "execution"],
    },
    {
        "id": "psych_teamwork",
        "key": "psych_teamwork",
        "text": "In a group project, you naturally end up:",
        "options": [
            {"label": "A", "text": "Researching and finding the right approach", "weights": {"analytical": 2.0, "strategic": 0.5}},
            {"label": "B", "text": "Coming up with ideas nobody else thought of", "weights": {"creative": 2.0}},
            {"label": "C", "text": "Making sure things actually ship on time", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Keeping the team aligned and unblocked", "weights": {"leadership": 1.5, "social": 1.0, "communication": 0.5}},
        ],
        "primary_dimensions": ["creative", "social", "leadership"],
    },
    {
        "id": "psych_frustration",
        "key": "psych_frustration",
        "text": "Which of these frustrates you most?",
        "options": [
            {"label": "A", "text": "Decisions made without evidence", "weights": {"analytical": 2.0, "strategic": 0.5}},
            {"label": "B", "text": "Being told to follow a rigid process", "weights": {"creative": 2.0}},
            {"label": "C", "text": "Endless discussions with no action", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Working in isolation without collaboration", "weights": {"social": 1.5, "communication": 0.5}},
        ],
        "primary_dimensions": ["analytical", "creative"],
    },
    {
        "id": "psych_crisis",
        "key": "psych_crisis",
        "text": "A project you're on is falling behind. Your first move?",
        "options": [
            {"label": "A", "text": "Find the bottleneck - what's actually broken?", "weights": {"analytical": 2.0, "technical": 0.5}},
            {"label": "B", "text": "Rethink the approach entirely", "weights": {"creative": 1.5, "strategic": 0.5}},
            {"label": "C", "text": "Cut scope and prioritize ruthlessly", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Rally the team and redistribute work", "weights": {"leadership": 1.5, "social": 1.0}},
        ],
        "primary_dimensions": ["execution", "leadership"],
    },
    {
        "id": "psych_energy",
        "key": "psych_energy",
        "text": "Which type of work gives you the most energy?",
        "options": [
            {"label": "A", "text": "Cracking a problem nobody else could solve", "weights": {"analytical": 1.5, "technical": 1.0}},
            {"label": "B", "text": "Building something from zero", "weights": {"creative": 1.5, "technical": 1.0}},
            {"label": "C", "text": "Shipping something people actually use", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Winning someone over or closing a deal", "weights": {"social": 1.5, "communication": 1.0}},
        ],
        "primary_dimensions": ["analytical", "creative", "execution", "social", "technical"],
    },
    {
        "id": "psych_learning",
        "key": "psych_learning",
        "text": "When you're learning something new, you prefer to:",
        "options": [
            {"label": "A", "text": "Read the docs and understand the theory first", "weights": {"analytical": 1.5, "technical": 0.5}},
            {"label": "B", "text": "Experiment and figure it out by doing", "weights": {"creative": 1.5, "execution": 0.5}},
            {"label": "C", "text": "Follow a step-by-step tutorial", "weights": {"execution": 2.0}},
            {"label": "D", "text": "Ask someone who already knows", "weights": {"social": 1.5, "communication": 0.5}},
        ],
        "primary_dimensions": ["analytical", "social"],
    },
    {
        "id": "psych_success",
        "key": "psych_success",
        "text": "Five years from now, what does success look like?",
        "options": [
            {"label": "A", "text": "Being the go-to expert in a deep field", "weights": {"analytical": 1.5, "technical": 1.0}},
            {"label": "B", "text": "Having built or launched something original", "weights": {"creative": 2.0, "execution": 0.5}},
            {"label": "C", "text": "Running a high-performance team or operation", "weights": {"leadership": 2.0, "execution": 0.5}},
            {"label": "D", "text": "Leading and inspiring a large group of people", "weights": {"leadership": 1.5, "communication": 1.0}},
        ],
        "primary_dimensions": ["analytical", "creative", "leadership", "technical"],
    },
    {
        "id": "psych_feedback",
        "key": "psych_feedback",
        "text": "When someone critiques your work, your honest reaction is:",
        "options": [
            {"label": "A", "text": "Show me the specifics - I want to understand exactly what's off", "weights": {"analytical": 2.0}},
            {"label": "B", "text": "Interesting - I'll rethink my approach", "weights": {"creative": 1.5, "strategic": 0.5}},
            {"label": "C", "text": "Fair - let me fix it right now", "weights": {"execution": 2.0}},
            {"label": "D", "text": "I appreciate that - let's discuss how to improve together", "weights": {"social": 1.5, "communication": 0.5}},
        ],
        "primary_dimensions": ["analytical", "execution"],
    },
    {
        "id": "psych_communication",
        "key": "psych_communication",
        "text": "When you need to get a complex idea across to a group, you:",
        "options": [
            {"label": "A", "text": "Structure it clearly with data and a logical flow", "weights": {"communication": 1.5, "analytical": 1.0}},
            {"label": "B", "text": "Tell a story around it - metaphors, narrative, examples", "weights": {"communication": 2.0, "creative": 0.5}},
            {"label": "C", "text": "Keep it short. One slide. Three bullets.", "weights": {"execution": 1.5, "communication": 0.5}},
            {"label": "D", "text": "Check in with the room - read the energy and adapt", "weights": {"social": 1.5, "leadership": 0.5}},
        ],
        "primary_dimensions": ["communication", "analytical"],
    },
    {
        "id": "psych_strategy",
        "key": "psych_strategy",
        "text": "Your team is planning for the next 12 months. Your instinct is to:",
        "options": [
            {"label": "A", "text": "Map the competitive landscape and find gaps", "weights": {"strategic": 2.0, "analytical": 0.5}},
            {"label": "B", "text": "Anchor on a bold vision and work backward", "weights": {"strategic": 1.5, "creative": 1.0}},
            {"label": "C", "text": "Break goals into quarterly milestones and own one", "weights": {"execution": 2.0, "leadership": 0.5}},
            {"label": "D", "text": "Survey the team - the best plan comes from the people doing the work", "weights": {"leadership": 1.5, "social": 1.0}},
        ],
        "primary_dimensions": ["strategic", "execution", "leadership"],
    },
]


FLEX_QUESTIONS: list[dict] = [
    {
        "id": "flex_best_project",
        "key": "flex_best_project",
        "text": "Describe your best project or experience in 1-2 sentences — what did you build or do, and who was it for?",
        "options": [],
        "primary_dimensions": [],
        "always_include": True,
        "text_input": True,
        "placeholder": "e.g. Built an automated reporting tool for a fashion brand that cut weekly manual work from 5 hours to 20 minutes",
    },
    {
        "id": "flex_outcome",
        "key": "flex_outcome",
        "text": "What was the result or impact of that?",
        "options": [],
        "primary_dimensions": [],
        "always_include": True,
        "text_input": True,
        "placeholder": "e.g. The client renewed and referred two others, or grew Instagram from 2k to 8k in 3 months",
    },
]


def get_question_by_id(question_id: str) -> dict | None:
    for q in PSYCHOMETRIC_QUESTIONS:
        if q["id"] == question_id:
            return q
    return None


def get_option_weights(question_id: str, selected_label: str) -> dict[str, float]:
    """Return the dimension weights for a selected option."""
    q = get_question_by_id(question_id)
    if not q:
        return {}
    for opt in q["options"]:
        if opt["label"] == selected_label:
            return dict(opt["weights"])
    return {}
