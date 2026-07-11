"""
test_scoping.py — isolation test for Rung 6.5a per-user scoping.

Runs against the LOCAL Postgres (DATABASE_URL). Proves:
  - two different chats can each save the SAME announcement (the cross-user
    collision that a global unique hash would silently drop),
  - re-saving the same text for the SAME chat is still a per-user no-op,
  - get_upcoming / get_undated never leak one chat's rows to another,
  - board tokens are stable + resolvable; unknown tokens resolve to None.

Standalone, no pytest: `python test_scoping.py`. Cleans up its own rows.
"""
from datetime import date, timedelta

import db

CHAT_A = -100001                 # synthetic ids that won't collide with real chats
CHAT_B = -100002
SHARED = "TEST-SCOPING shared: assignment 9 is due soon on the portal."
A_ONLY = "TEST-SCOPING alpha: lab report is due soon."
UNDATED_A = "TEST-SCOPING gamma: fee payment, date to be announced."

FUTURE = date.today() + timedelta(days=30)


def _cleanup():
    with db._connect() as conn:
        conn.execute("DELETE FROM deadlines WHERE chat_id IN (%s, %s)", (CHAT_A, CHAT_B))
        conn.execute("DELETE FROM boards WHERE chat_id IN (%s, %s)", (CHAT_A, CHAT_B))


def _titles(rows):
    return {r["title"] for r in rows}


def main():
    db.init_db()
    _cleanup()
    try:
        # cross-user collision: identical raw_text, two chats, both save
        assert db.save_deadline({"title": "Shared", "deadline": FUTURE}, SHARED, CHAT_A) == "saved"
        assert db.save_deadline({"title": "Shared", "deadline": FUTURE}, SHARED, CHAT_B) == "saved"
        print("PASS: identical text saved for two different chats")

        # per-user retry safety: same chat + same text is a no-op
        assert db.save_deadline({"title": "Shared", "deadline": FUTURE}, SHARED, CHAT_A) == "duplicate"
        print("PASS: re-saving same text for the same chat is a duplicate")

        # a row unique to A, plus an undated row for A
        assert db.save_deadline({"title": "AlphaOnly", "deadline": FUTURE}, A_ONLY, CHAT_A) == "saved"
        assert db.save_deadline({"title": "UndatedA", "deadline": None}, UNDATED_A, CHAT_A) == "saved"

        # isolation: each chat sees only its own rows
        up_a, up_b = _titles(db.get_upcoming(CHAT_A)), _titles(db.get_upcoming(CHAT_B))
        assert up_a == {"Shared", "AlphaOnly"}, up_a
        assert up_b == {"Shared"}, up_b
        print(f"PASS: get_upcoming isolation  A={sorted(up_a)}  B={sorted(up_b)}")

        und_a, und_b = _titles(db.get_undated(CHAT_A)), _titles(db.get_undated(CHAT_B))
        assert und_a == {"UndatedA"}, und_a
        assert und_b == set(), und_b
        print(f"PASS: get_undated isolation  A={sorted(und_a)}  B={sorted(und_b)}")

        # search is scoped too
        assert _titles(db.get_upcoming(CHAT_A, q="alpha")) == {"AlphaOnly"}
        assert _titles(db.get_upcoming(CHAT_B, q="alpha")) == set()
        print("PASS: keyword search stays within a chat")

        # board tokens: stable, resolvable, unknown -> None
        t1, t2 = db.get_or_create_board(CHAT_A), db.get_or_create_board(CHAT_A)
        assert t1 and t1 == t2, (t1, t2)
        assert db.get_chat_id_for_token(t1) == CHAT_A
        assert db.get_or_create_board(CHAT_B) != t1
        assert db.get_chat_id_for_token("no-such-token") is None
        print("PASS: board token stable, resolvable, unknown -> None")

        print("\nALL SCOPING TESTS PASSED")
    finally:
        _cleanup()


if __name__ == "__main__":
    main()
