MATCH (p:Party {uid: $tenant + '|' + $party})-[:HAS_IDENTIFIER]->(i:Identifier)<-[:HAS_IDENTIFIER]-(other:Party)
WHERE other.uid <> p.uid
RETURN other.node_id AS party, i.subtype AS identifier_type, count(*) AS shared_cnt
ORDER BY shared_cnt DESC, party
