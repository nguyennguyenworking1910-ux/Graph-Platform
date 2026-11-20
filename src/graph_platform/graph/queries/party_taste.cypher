MATCH (p:Party {uid: $tenant + '|' + $party})
MATCH (r:Item|Party)
WHERE r.uid IN [x IN $items | $tenant + '|' + x]
WITH p, collect(r) AS suggested, head(collect(r)) AS one
MATCH (p)-[e:PURCHASES|TRANSACTS]->(s)
WHERE e.event_time < $as_of AND labels(s) = labels(one) AND s.subtype = one.subtype AND NOT s IN suggested
WITH p, suggested, collect(DISTINCT s) AS mine
UNWIND suggested AS r
MATCH (o:Party)-[e:PURCHASES|TRANSACTS]->(r)
WHERE o <> p AND e.event_time < $as_of
WITH DISTINCT o, mine, suggested
MATCH (o)-[e:PURCHASES|TRANSACTS]->(x)
WHERE e.event_time < $as_of AND (x IN mine OR x IN suggested)
WITH o, mine, collect(DISTINCT x) AS chosen
WITH o, [x IN chosen WHERE x IN mine | x.node_id] AS same, [x IN chosen WHERE NOT x IN mine | x.node_id] AS wanted
WHERE size(same) > 0
RETURN o.node_id AS node_id, same, wanted
ORDER BY size(same) DESC, size(wanted) DESC, node_id
LIMIT 4
