"""Parameterised Cypher statements. Values are always bound as ``$parameters``.

No endpoint accepts or forwards arbitrary Cypher; these are the only statements
the application runs.
"""

from __future__ import annotations

# --- reference data ---------------------------------------------------------

SEED_TEMPLATE = """
MERGE (t:EmailTemplate {id: $id})
ON CREATE SET t.created_at = $now
SET t.version = $version, t.name = $name, t.kind = $kind,
    t.onboarding_type = $onboarding_type, t.subject = $subject, t.text = $text,
    t.html = $html, t.required_fields = $required_fields, t.updated_at = $now
"""

SEED_AGENT = """
MERGE (a:Agent {id: $id})
ON CREATE SET a.created_at = $now
SET a.name = $name, a.kind = $kind, a.description = $description
"""

SEED_PROVIDER = """
MERGE (p:EmailProvider {id: $id})
ON CREATE SET p.created_at = $now
SET p.name = $id, p.role = $role, p.enabled = $enabled, p.updated_at = $now
"""

GET_TEMPLATE = "MATCH (t:EmailTemplate {id: $id}) RETURN properties(t) AS t"

# --- onboarding -------------------------------------------------------------

GET_ONBOARDING_NODE = "MATCH (ob:Onboarding {id: $id}) RETURN ob.id AS id"

CREATE_ONBOARDING = """
MERGE (o:Organisation {id: $org_id})
ON CREATE SET o.name = $org_name, o.created_at = $now
MERGE (p:Person {id: $person_id})
ON CREATE SET p.created_at = $now
SET p.name = $person_name, p.email = $person_email
MERGE (t:Team {id: $team_id})
ON CREATE SET t.name = $team_name, t.created_at = $now
MERGE (t)-[:PART_OF]->(o)
MERGE (p)-[:MEMBER_OF]->(t)
CREATE (ob:Onboarding)
SET ob = $onboarding
CREATE (p)-[:UNDERGOES]->(ob)
CREATE (ob)-[:FOR_ORGANISATION]->(o)
"""

_ONBOARDING_RETURN = """
MATCH (p:Person)-[:UNDERGOES]->(ob)-[:FOR_ORGANISATION]->(o:Organisation)
MATCH (t:Team {id: ob.team_id})
RETURN properties(ob) AS ob, properties(p) AS p, properties(o) AS o, properties(t) AS t
"""

GET_ONBOARDING = "MATCH (ob:Onboarding {id: $id})" + _ONBOARDING_RETURN

LIST_ONBOARDINGS = (
    "MATCH (ob:Onboarding) WHERE $type IS NULL OR ob.type = $type"
    + _ONBOARDING_RETURN
    + " ORDER BY ob.start_date, ob.id LIMIT $limit"
)

# --- drafts -----------------------------------------------------------------

ACTIVE_DRAFT_FOR_TEMPLATE = """
MATCH (:Onboarding {id: $onboarding_id})-[:HAS_DRAFT]->(d:EmailDraft {template_id: $template_id})
WHERE d.status <> 'CANCELLED'
RETURN d.id AS id LIMIT 1
"""

CREATE_ONBOARDING_DRAFT = """
MATCH (ob:Onboarding {id: $onboarding_id})
MATCH (tpl:EmailTemplate {id: $template_id})
MATCH (a:Agent {id: $agent_id})
CREATE (d:EmailDraft)
SET d = $draft
CREATE (ob)-[:HAS_DRAFT]->(d)
MERGE (ob)-[:USES_TEMPLATE]->(tpl)
MERGE (a)-[pf:PREPARED_FOR]->(ob)
ON CREATE SET pf.first_at = $now
SET pf.last_at = $now, pf.drafts = coalesce(pf.drafts, 0) + 1, ob.status = 'IN_PROGRESS'
RETURN d.id AS id
"""

CREATE_STANDALONE_DRAFT = "CREATE (d:EmailDraft) SET d = $draft RETURN d.id AS id"

GET_DRAFT = "MATCH (d:EmailDraft {id: $id}) RETURN properties(d) AS d"

UPDATE_DRAFT = "MATCH (d:EmailDraft {id: $id}) SET d += $props RETURN properties(d) AS d"

DRAFTS_BY_STATUS = """
MATCH (d:EmailDraft) WHERE d.status IN $statuses
RETURN properties(d) AS d ORDER BY d.created_at, d.id LIMIT $limit
"""

UNSENT_ONBOARDING_DRAFTS = """
MATCH (:Onboarding)-[:HAS_DRAFT]->(d:EmailDraft) WHERE d.status IN $statuses
RETURN properties(d) AS d ORDER BY d.created_at, d.id LIMIT $limit
"""

ONBOARDING_DRAFTS = """
MATCH (:Onboarding {id: $id})-[:HAS_DRAFT]->(d:EmailDraft)
RETURN properties(d) AS d ORDER BY d.created_at, d.id
"""

# --- approvals --------------------------------------------------------------

DRAFT_APPROVALS = """
MATCH (:EmailDraft {id: $id})-[:HAS_APPROVAL]->(a:Approval)
RETURN properties(a) AS a ORDER BY a.decided_at, a.id
"""

INVALIDATE_APPROVALS = """
MATCH (:EmailDraft {id: $id})-[:HAS_APPROVAL]->(a:Approval)
WHERE a.valid = true
SET a.valid = false, a.invalidated_at = $now, a.invalidated_reason = $reason
RETURN count(a) AS n
"""

CREATE_APPROVAL = """
MATCH (d:EmailDraft {id: $draft_id})
CREATE (a:Approval)
SET a = $approval
CREATE (d)-[:HAS_APPROVAL]->(a)
"""

# --- deliveries -------------------------------------------------------------

CREATE_DELIVERY = """
MATCH (d:EmailDraft {id: $draft_id})
CREATE (x:EmailDelivery)
SET x = $delivery
CREATE (d)-[:HAS_DELIVERY]->(x)
"""

GET_DELIVERY = "MATCH (x:EmailDelivery {id: $id}) RETURN properties(x) AS x"

UPDATE_DELIVERY = "MATCH (x:EmailDelivery {id: $id}) SET x += $props"

LINK_DELIVERY_PROVIDER = """
MATCH (x:EmailDelivery {id: $id})
MERGE (p:EmailProvider {id: $provider})
ON CREATE SET p.name = $provider, p.created_at = $now
MERGE (x)-[r:SENT_THROUGH]->(p)
SET r.attempts = coalesce(r.attempts, 0) + 1, r.last_outcome = $outcome,
    r.provider_message_id = $message_id, r.last_attempt_at = $now
"""

DRAFT_DELIVERIES = """
MATCH (:EmailDraft {id: $id})-[:HAS_DELIVERY]->(x:EmailDelivery)
RETURN properties(x) AS x ORDER BY x.sequence
"""

FAILED_DELIVERIES = """
MATCH (:EmailDraft)-[:HAS_DELIVERY]->(x:EmailDelivery)
WHERE x.status IN ['FAILED', 'UNKNOWN']
RETURN properties(x) AS x ORDER BY x.created_at DESC, x.id LIMIT $limit
"""

INTERRUPTED_DELIVERIES = """
MATCH (d:EmailDraft {status: 'SENDING'})-[:HAS_DELIVERY]->(x:EmailDelivery {status: 'PENDING'})
RETURN d.id AS draft_id, x.id AS delivery_id
"""

# --- diagnostics ------------------------------------------------------------

LABEL_COUNTS = """
UNWIND $labels AS label
CALL (label) {
  MATCH (n) WHERE label IN labels(n) RETURN count(n) AS n
}
RETURN label, n
"""
