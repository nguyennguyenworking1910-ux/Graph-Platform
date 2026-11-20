MATCH (n:Item|Party)
WHERE n.uid IN [x IN $nodes | $tenant + '|' + x]
RETURN n.node_id AS node_id, labels(n)[0] AS node_type, n.subtype AS subtype, coalesce(n.trade_name, n.title) AS name
