"""Customer 360: one profile per resolved party across every business domain of the tenant."""

from __future__ import annotations

from graph_platform.branches.runner import Context, Result


def run(ctx: Context) -> Result:
    snap = ctx.snapshot(ctx.contract.as_of)
    table = snap.con.sql(
        """WITH ident AS (
            SELECT src_id AS party_id,
                   count(DISTINCT dst_id) FILTER (split_part(dst_id, ':', 2) = 'phone') AS phone_nuniq,
                   count(DISTINCT dst_id) FILTER (split_part(dst_id, ':', 2) = 'email') AS email_nuniq
            FROM e_HAS_IDENTIFIER GROUP BY 1),
        dev AS (SELECT src_id AS party_id, count(DISTINCT dst_id) AS device_nuniq FROM e_USES GROUP BY 1),
        ev AS (
            SELECT a.src_id AS party_id,
                   count(*) FILTER (e.subtype = 'loan') AS loan_cnt,
                   coalesce(sum(e.amount) FILTER (e.subtype = 'loan'), 0) AS loan_amt_sum,
                   count(*) FILTER (e.subtype = 'application') AS application_cnt
            FROM e_APPLIES_FOR a JOIN n_Event e ON e.node_id = a.dst_id GROUP BY 1),
        acct AS (
            SELECT o.src_id AS party_id,
                   count(*) FILTER (a.subtype = 'policy') AS policy_cnt,
                   count(*) FILTER (a.subtype = 'savings') AS savings_account_cnt
            FROM e_OWNS o JOIN n_Account a ON a.node_id = o.dst_id GROUP BY 1),
        buy AS (
            SELECT p.src_id AS party_id,
                   count(*) FILTER (i.subtype = 'trip') AS trip_cnt,
                   count(*) FILTER (i.subtype = 'movie') AS movie_cnt,
                   coalesce(sum(p.amount), 0) AS purchase_amt_sum, max(p.event_time) AS last_purchase_time
            FROM e_PURCHASES p JOIN n_Item i ON i.node_id = p.dst_id GROUP BY 1),
        pay AS (
            SELECT party_id, count(*) FILTER (dir = 'out') AS txn_out_cnt, count(*) FILTER (dir = 'in') AS txn_in_cnt,
                   coalesce(sum(amount) FILTER (dir = 'out'), 0) AS amt_out_sum,
                   coalesce(sum(amount) FILTER (dir = 'in'), 0) AS amt_in_sum,
                   max(event_time) AS last_txn_time
            FROM (SELECT src_id AS party_id, 'out' AS dir, amount, event_time FROM e_TRANSACTS
                  UNION ALL SELECT dst_id, 'in', amount, event_time FROM e_TRANSACTS) GROUP BY 1),
        peers AS (SELECT n AS idx, count(*) AS peer_cnt FROM ranked GROUP BY 1)
        SELECT p.party_id, p.subtype, p.valid_from AS first_seen_time, p.domains, len(p.domains) AS domain_cnt, p.source_cnt,
               p.city, p.occupation, p.income, date_diff('year', p.birth_date, CURRENT_DATE) AS age,
               coalesce(phone_nuniq, 0) AS phone_nuniq, coalesce(email_nuniq, 0) AS email_nuniq, coalesce(device_nuniq, 0) AS device_nuniq,
               coalesce(loan_cnt, 0) AS loan_cnt, coalesce(loan_amt_sum, 0) AS loan_amt_sum,
               coalesce(application_cnt, 0) AS application_cnt,
               coalesce(policy_cnt, 0) AS policy_cnt, coalesce(savings_account_cnt, 0) AS savings_account_cnt,
               coalesce(trip_cnt, 0) AS trip_cnt, coalesce(movie_cnt, 0) AS movie_cnt, coalesce(purchase_amt_sum, 0) AS purchase_amt_sum,
               coalesce(txn_out_cnt, 0) AS txn_out_cnt, coalesce(txn_in_cnt, 0) AS txn_in_cnt,
               coalesce(amt_out_sum, 0) AS amt_out_sum, coalesce(amt_in_sum, 0) AS amt_in_sum,
               greatest(last_purchase_time, last_txn_time) AS last_activity_time, coalesce(peer_cnt, 0) AS peer_cnt
        FROM (SELECT *, node_id AS party_id FROM party) p
        LEFT JOIN ident USING (party_id) LEFT JOIN dev USING (party_id) LEFT JOIN ev USING (party_id)
        LEFT JOIN acct USING (party_id) LEFT JOIN buy USING (party_id) LEFT JOIN pay USING (party_id)
        LEFT JOIN peers ON peers.idx = p.idx
        ORDER BY party_id"""
    ).pl()  # fmt: skip
    person = table.filter(table["subtype"] == "person")
    metrics = {
        "party_cnt": table.height,
        "person_cnt": person.height,
        "multi_domain_ratio": float((person["domain_cnt"] >= 2).mean()),
        "domain_cnt_avg": float(person["domain_cnt"].mean()),
        "source_cnt_avg": float(person["source_cnt"].mean()),
    }
    return Result(metrics, table)
