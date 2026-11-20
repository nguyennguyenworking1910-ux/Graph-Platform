MATCH path = (a:Party {uid: $tenant + '|' + $src})-[:TRANSACTS*1..3]->(b:Party {uid: $tenant + '|' + $dst})
RETURN length(path) AS hops, count(*) AS path_cnt
ORDER BY hops
