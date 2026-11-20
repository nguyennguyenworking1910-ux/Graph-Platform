"""Pictures of the graph, read from Neo4j: the network around one party, and a group with what it shares.

Both return `{"nodes", "edges", "mode", "story"}`: what to draw and the plain sentences that say what the picture shows.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time

from graph_platform.graph import GraphStore, named_query
from graph_platform.ui.names import EVENT, money, node_label, party_name

CANDIDATES, PARTIES_SHOWN, ITEMS_SHOWN, MANY_CHARGEBACKS = 40, 12, 4, 5
KIND = {"Device": "device", "Identifier": "identifier", "Item": "item", "Account": "item"}
EDGE = {"USES": "device", "HAS_IDENTIFIER": "identifier", "PURCHASES": "purchase", "OWNS": "purchase"}
RELATION = {"owns_business": "chủ cửa hàng"}
PHRASE = {
    "late": "đã trễ hạn từ 30 ngày", "fraud": "từng bị báo gian lận", "churn": "đã đóng tài khoản",
    "chargeback": "bị đòi hoàn tiền nhiều lần", "claim": "đã yêu cầu bồi thường",
}  # fmt: skip
ADVERSE = frozenset({"late", "fraud", "chargeback"})


def _ask(store: GraphStore, tenant: str, name: str, **params: object) -> list[dict]:
    return store.query(tenant, named_query(name), params).to_dicts()


def _cut(as_of: date | None) -> datetime:
    """Pictures of a result show the graph as it was on the result's date; everything else shows it as it is now."""
    return datetime.combine(as_of, time()) if as_of else datetime.now()


def _event(row: dict) -> tuple[str, str] | None:
    subtype, cnt, dpd = row["subtype"], row["event_cnt"], row["dpd_max"] or 0
    if subtype in ("loan", "merchant_loan"):
        return ("late", f"trễ hạn {dpd} ngày") if dpd >= 30 else None
    if subtype == "chargeback" and cnt < MANY_CHARGEBACKS:
        return None
    return {
        "churn": ("churn", "đã đóng tài khoản"),
        "fraud_case": ("fraud", "từng bị báo gian lận"),
        "chargeback": ("chargeback", f"{cnt} lần bị đòi hoàn tiền"),
        "claim": ("claim", f"{cnt} lần yêu cầu bồi thường"),
    }.get(subtype)


def _events(store: GraphStore, tenant: str, parties: list[str], as_of: date | None) -> dict[str, dict[str, str]]:
    """What is on record for each party before `as_of`: kind -> words."""
    out: dict[str, dict[str, str]] = {}
    for row in _ask(store, tenant, "party_events", parties=parties, as_of=_cut(as_of)):
        if found := _event(row):
            out.setdefault(row["party_id"], {})[found[0]] = found[1]
    return out


def _parties(store, tenant, ids: list[str], events: dict, watch: frozenset[str], focus: str | None = None) -> list[dict]:
    rows = {r["node_id"]: r for r in _ask(store, tenant, "graph_nodes", nodes=ids)}
    nodes = []
    for i in (i for i in ids if i in rows):
        on_record = {k: v for k, v in events.get(i, {}).items() if k in watch or i == focus}
        nodes.append(
            {
                "id": i, "kind": "person" if rows[i]["subtype"] == "person" else "merchant",
                "label": party_name(i, rows[i]["subtype"], rows[i]["name"]), "note": ", ".join(on_record.values()),
                "flag": bool(set(on_record) & watch), "focus": i == focus, "open": i != focus,
            }
        )  # fmt: skip
    return nodes


def _links(store: GraphStore, tenant: str, ids: list[str], as_of: date | None) -> list[dict]:
    return [
        {
            "source": r["src_id"], "target": r["dst_id"], "weight": r["edge_cnt"],
            "kind": "transfer" if r["edge_type"] == "TRANSACTS" else "refer",
            "label": money(r["amt_sum"] or 0) if r["edge_type"] == "TRANSACTS" else RELATION.get(r["relation"], "giới thiệu"),
        }
        for r in _ask(store, tenant, "party_links", parties=ids, as_of=_cut(as_of))
    ]  # fmt: skip


def _thing(row: dict) -> dict:
    return {
        "id": row["node_id"],
        "kind": KIND[row["node_type"]],
        "label": node_label(row["node_type"], row["subtype"], row["node_id"], row["name"]),
    }


def _cycle(edges: list[dict]) -> list[str]:
    """One closed loop of transfers through at least three parties, shortest first; empty when money never comes back."""
    out: dict[str, list[str]] = {}
    for e in edges:
        if e["kind"] == "transfer":
            out.setdefault(e["source"], []).append(e["target"])
    for start in out:
        paths = [[start]]
        for _ in range(4):
            paths = [[*p, n] for p in paths for n in out.get(p[-1], []) if n not in p]
            closed = [p for p in paths if len(p) >= 3 and start in out.get(p[-1], [])]
            if closed:
                return closed[0]
    return []


def party_network(store: GraphStore, tenant: str, party: str, as_of: date | None = None, watch: frozenset[str] = ADVERSE) -> dict:
    near = _ask(store, tenant, "party_neighbors", party=party, as_of=_cut(as_of))
    sharers = _ask(store, tenant, "party_sharers", party=party, as_of=_cut(as_of))
    peers = list(dict.fromkeys(r["node_id"] for r in near if r["node_type"] == "Party"))
    close = list(dict.fromkeys([*(r["node_id"] for r in sharers), *peers[:CANDIDATES]]))
    events = _events(store, tenant, [party, *close], as_of)
    flagged = [i for i in close if set(events.get(i, {})) & watch]
    shown = list(dict.fromkeys([*(r["node_id"] for r in sharers), *flagged, *close]))[:PARTIES_SHOWN]

    nodes = _parties(store, tenant, [party, *shown], events, watch, focus=party)
    edges = [e for e in _links(store, tenant, [party, *shown], as_of) if party in (e["source"], e["target"])]
    things = [r for r in near if r["node_type"] in KIND]
    # one of each kind of product first, then the most used
    items = [r for r in things if r["node_type"] == "Item"]
    seen = Counter()
    for r in items:
        seen[r["subtype"]] += 1
        r["nth"] = seen[r["subtype"]]
    items.sort(key=lambda r: r["nth"])
    for row in [r for r in things if r["node_type"] != "Item"] + items[:ITEMS_SHOWN]:
        nodes.append(_thing(row))
        label = f"{row['edge_cnt']} lần" if row["edge_type"] == "PURCHASES" and row["edge_cnt"] > 1 else ""
        edges.append({"source": party, "target": row["node_id"], "kind": EDGE[row["edge_type"]], "label": label, "weight": 1})
    drawn = {n["id"]: n["kind"] for n in nodes}
    edges += [
        {"source": r["node_id"], "target": r["via"], "kind": drawn[r["via"]], "label": "", "weight": 1}
        for r in sharers
        if r["via"] in drawn
    ]
    own = Counter(r["subtype"] for r in near if r["node_type"] == "Event" and r["subtype"] not in ("application", "deposit"))
    for subtype, cnt in own.items():
        nodes.append({"id": f"{party}|{subtype}", "kind": "item", "label": f"{cnt} {EVENT.get(subtype, subtype)}"})
        edges.append({"source": party, "target": f"{party}|{subtype}", "kind": "purchase", "label": "", "weight": 1})

    story = [f"{len(peers)} người và cửa hàng có giao dịch hoặc giới thiệu qua lại."] if peers else []
    for kind in sorted(watch):
        hit = sum(kind in events.get(i, {}) for i in close)
        if hit:
            story.append(f"{hit} trong {len(close)} người gần nhất {PHRASE[kind]}.")
    if sharers:
        story.append(f"Dùng chung thiết bị hoặc giấy tờ với {len({r['node_id'] for r in sharers})} người khác.")
    hidden = len(peers) - sum(i in peers for i in shown) + len(items[ITEMS_SHOWN:])
    if hidden:
        story.append(f"Hình vẽ người cần lưu ý trước, rồi tới quan hệ mạnh nhất; còn {hidden} quan hệ khác.")
    return {"nodes": nodes, "edges": edges, "mode": "party", "story": story}


def group_network(store: GraphStore, tenant: str, members: list[str], as_of: date | None = None, watch: frozenset[str] = ADVERSE) -> dict:
    nodes = _parties(store, tenant, members, _events(store, tenant, members, as_of), watch)
    edges = _links(store, tenant, members, as_of)
    name = {n["id"]: n["label"] for n in nodes}
    story = []
    loop = _cycle(edges)
    if loop:
        story.append("Tiền đi thành vòng khép kín: " + " → ".join(name[i] for i in [*loop, loop[0]]) + ".")
    # only what at least half of the group has in common
    for row in _ask(store, tenant, "party_shared", parties=members, member_cnt_min=max(2, len(members) // 2), as_of=_cut(as_of)):
        thing = _thing(row)
        nodes.append(thing)
        edges += [{"source": m, "target": row["node_id"], "kind": EDGE[row["edge_type"]], "label": "", "weight": 1} for m in row["members"]]
        verb = "cùng mua" if row["edge_type"] == "PURCHASES" else "dùng chung"
        story.append(f"{len(row['members'])} trong {len(members)} thành viên {verb} {thing['label']}.")
    return {"nodes": nodes, "edges": edges, "mode": "group", "story": story[:5]}


def recommendation_network(store: GraphStore, tenant: str, party: str, suggested: list[str], as_of: date | None = None) -> dict:
    """Why these suggestions: what the party already chose, who chose the same, and what those people chose next."""
    alike = _ask(store, tenant, "party_taste", party=party, items=suggested, as_of=_cut(as_of))
    chosen = list(dict.fromkeys(i for r in alike for i in r["same"]))[:ITEMS_SHOWN]
    rows = {r["node_id"]: r for r in _ask(store, tenant, "graph_nodes", nodes=[*chosen, *suggested])}
    nodes = _parties(store, tenant, [party, *(r["node_id"] for r in alike)], {}, frozenset(), focus=party)
    for i in (i for i in [*chosen, *suggested] if i in rows):
        kind = "merchant" if rows[i]["node_type"] == "Party" else KIND[rows[i]["node_type"]]
        label = node_label(rows[i]["node_type"], rows[i]["subtype"], i, rows[i]["name"])
        nodes.append({"id": i, "kind": kind, "label": label, "suggest": i in suggested, "open": kind == "merchant"})
    name = {n["id"]: n["label"] for n in nodes}
    edges = [{"source": party, "target": i, "kind": "purchase", "label": "", "weight": 1} for i in chosen]
    edges += [{"source": party, "target": i, "kind": "suggest", "label": "gợi ý", "weight": 1} for i in suggested if i in name]
    for r in alike:
        edges += [
            {"source": r["node_id"], "target": i, "kind": "purchase", "label": "", "weight": 1}
            for i in [*r["same"], *r["wanted"]]
            if i in name
        ]
    story = [f"Đã chọn: {', '.join(name[i] for i in chosen)}."] if chosen else ["Chưa tìm thấy người có lựa chọn giống khách này."]
    also = list(dict.fromkeys(i for r in alike for i in r["wanted"] if i in name))
    if also:
        story.append(f"{len(alike)} người có lựa chọn giống nhất còn chọn: {', '.join(name[i] for i in also)}.")
    return {"nodes": nodes, "edges": edges, "mode": "party", "story": story}
