MATCH (a:Party)-[:APPLIES_FOR]->(e:Event)
WHERE a.uid IN [x IN $parties | $tenant + '|' + x] AND e.event_time < $as_of
OPTIONAL MATCH (f:Event)-[:RELATES_TO]->(e)
WHERE f.event_time < $as_of
RETURN a.node_id AS party_id, e.subtype AS subtype, count(DISTINCT e) AS event_cnt, max(f.days_past_due) AS dpd_max
