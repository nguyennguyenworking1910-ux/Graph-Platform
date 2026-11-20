MATCH (a:Party)-[r:TRANSACTS|REFERS]->(b:Party)
WHERE a.uid IN [x IN $parties | $tenant + '|' + x] AND b.uid IN [x IN $parties | $tenant + '|' + x] AND r.event_time < $as_of
RETURN a.node_id AS src_id, b.node_id AS dst_id, type(r) AS edge_type, count(*) AS edge_cnt, sum(r.amount) AS amt_sum,
       max(r.relation) AS relation
ORDER BY src_id, dst_id, edge_type
