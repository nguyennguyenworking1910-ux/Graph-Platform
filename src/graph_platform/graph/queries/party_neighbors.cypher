MATCH (p:Party {uid: $tenant + '|' + $party})-[r]-(n)
WHERE r.event_time < $as_of
WITH n, type(r) AS edge_type, count(*) AS edge_cnt, sum(r.amount) AS amt_sum
RETURN n.node_id AS node_id, labels(n)[0] AS node_type, n.subtype AS subtype, coalesce(n.trade_name, n.title) AS name,
       edge_type, edge_cnt, amt_sum
ORDER BY edge_cnt DESC, node_id
