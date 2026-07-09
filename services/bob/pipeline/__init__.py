"""Bob's staged discovery pipeline.

Replaces the ReAct loop as scheduler (run-36 forensics: 66% of harvested
results never touched, 8% orphaned mid-flight, voluntary stop at 8/10).
The orchestrator owns batching, retries, loop-until-N and termination;
the LLM is only called for judgment (extraction, scoring, tie-breaks),
in batches, adjacent to the source text.

Stages: harvest -> extract -> verify -> score -> contact -> assemble.
All state lives in bob_raw_items / bob_opportunities, so runs are resumable
and every discarded opportunity records its reject stage + reason.
"""
