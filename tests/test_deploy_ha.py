"""IN-N01 (B2C audit 30 Sep, recon): two replicas must not share one node.

The HA fix raised replicas to 2 with a PDB, but nothing spread them: on 30 Sep
both production frontend pods ran on the same node, so losing that node still
took the site down. The production deploy workflow now patches a hostname
topologySpreadConstraint onto the Deployment before every rollout.
"""
import json
import pathlib
import re

WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / ".github" / "workflows" / "deploy.yml"


def test_prod_deploy_spreads_replicas_across_nodes():
    text = WORKFLOW.read_text()
    m = re.search(r"'(\{\"spec\":\{\"template\":\{\"spec\":\{\"topologySpreadConstraints\".*?)'\n", text)
    assert m, "deploy.yml must patch topologySpreadConstraints onto the Deployment"
    patch = json.loads(m.group(1))
    (c,) = patch["spec"]["template"]["spec"]["topologySpreadConstraints"]
    assert c["topologyKey"] == "kubernetes.io/hostname"
    assert c["whenUnsatisfiable"] == "ScheduleAnyway"  # never blocks a rollout
    assert c["labelSelector"]["matchLabels"] == {"app": "job-outreach-svc"}
    # The patch must run before the new image rolls out.
    assert text.index("topologySpreadConstraints") < text.index("kubectl set image")
