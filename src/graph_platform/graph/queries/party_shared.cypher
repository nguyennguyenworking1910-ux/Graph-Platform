MATCH (a:Party)-[r:USES|HAS_IDENTIFIER|PURCHASES]->(x)
WHERE a.uid IN [y IN $parties | $tenant + '|' + y] AND r.event_time < $as_of
WITH x, type(r) AS edge_type, collect(DISTINCT a.node_id) AS members
WHERE size(members) >= $member_cnt_min
RETURN x.node_id AS node_id, labels(x)[0] AS node_type, x.subtype AS subtype, x.title AS name, edge_type, members
ORDER BY size(members) DESC, node_id
LIMIT 5
