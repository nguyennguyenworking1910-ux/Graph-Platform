MATCH (p:Party {uid: $tenant + '|' + $party})-[a:USES|HAS_IDENTIFIER]->(x)<-[b:USES|HAS_IDENTIFIER]-(o:Party)
WHERE o.uid <> p.uid AND a.event_time < $as_of AND b.event_time < $as_of
RETURN DISTINCT x.node_id AS via, o.node_id AS node_id
ORDER BY node_id
LIMIT 8
