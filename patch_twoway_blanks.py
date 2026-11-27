#!/usr/bin/env python3
"""
patch_twoway_blanks.py — run once.

RealGM sometimes names a team without linking it, which leaves a blank in the
sentence: "The Los Angeles Lakers assigned Cameron Carr to the ." Those 20
moves were filed as 'other'. Now any team slot may be empty: the move is still
filed by its kind, with that team left unknown, and the teams that are there
still land in the right columns. Rows already stored are refiled on the next
run (a re-seen move now updates its kind and teams, not only its player).
"""
import os
import sys

HOME = os.environ.get("BOXANDONE_HOME", os.path.expanduser("~/boxandone"))
PATH = os.path.join(HOME, "ingest_twoway.py")
src = open(PATH).read()
if "def form_regex" in src:
    print("  ingest_twoway.py already patched")
    sys.exit()

FORMS = r'''# each sentence with its links replaced by {P} (the player) and {T} (a team).
# Every {T} may be blank (RealGM names some teams without a link); the roles
# say which column each {T} slot fills, in order.
FORMS = [
    ("rights_trade", r"{P} was inactive and had his rights acquired by the {T} from the {T} in a trade", ("to", "from")),
    ("gl_trade", r"{P} was acquired by the {T} from the {T} in a trade", ("to", "from")),
    ("return_from_callup", r"{P} returned to the {T} after an NBA call up", ("to",)),
    ("returned", r"{P} returned to the {T}", ("to",)),
    ("gl_waive", r"The {T} placed the contract of {P} on waivers", ("from",)),
    ("twoway_sign", r"{P} signed a two-way contract with the {T} and {T}", ("nba", "to")),
    ("allocate", r"{P}, whose NBA Rights is held by the {T}, was allocated to the {T}", ("nba", "to")),
    ("allocate", r"{P} was allocated to the {T} from the {T}", ("to", "nba")),
    ("allocate", r"{P} was allocated to the {T}", ("to",)),
    ("deallocate", r"{P} was deallocated from the {T}", ("from",)),
    ("tryout", r"{P} tried out with the {T}", ("to",)),
    ("draft", r"The {T} selected {P} in Round (?P<rnd>\d+) with Pick (?P<pick>\d+) in the (?P<yr>\d+) (?P<dr>Annual|International) G League Draft", ("to",)),
    ("assign", r"The {T} assigned {P} to the {T}", ("nba", "to")),
    ("recall", r"The {T} recalled {P} from the {T}", ("nba", "from")),
    ("call_up", r"The {T} called up {P} from the {T}", ("nba", "from")),
    ("twoway_end", r"The {T} ended the two-way contract of {P}", ("nba",)),
    ("gl_terminate", r"The G League terminated the contract of {P} from the {T}", ("from",)),
    ("pool_claim", r"The {T} claimed {P} from the player pool", ("to",)),
    ("gl_claim", r"The {T} made a successful waiver claim for the contract of {P}", ("to",)),
    ("deactivate", r"The {T} deactivated {P}", ("from",)),
]


def form_regex(pat):
    """A form as a regex in which every team slot is optional (with the space
    in front of it) and a stray space before punctuation is allowed."""
    out, n = "", 0
    for i, part in enumerate(pat.split("{T}")):
        part = part.replace("{P}", r"\{P\}")
        if i:
            out = out.rstrip(" ")
            out += r"(?: (?P<t%d>\{T\}))?" % n
            n += 1
            part = " ?" + part.lstrip(" ") if part[:1] == " " else " ?" + part
        out += part
    return re.compile(out)


FORM_RES = [(kind, form_regex(pat), roles) for kind, pat, roles in FORMS]


def parse_transactions(html):'''

LOOP_OLD = '''            for kind, pat, roles in FORMS:
                fm = re.match(pat, form)
                if not fm:
                    continue
                row["kind"] = kind
                for role, team in zip(roles, teams):
                    row[{"to": "to_team", "from": "from_team", "nba": "nba_team"}[role]] = team'''
LOOP_NEW = '''            for kind, rx, roles in FORM_RES:
                fm = rx.match(form)
                if not fm:
                    continue
                row["kind"] = kind
                left = list(teams)
                for n, role in enumerate(roles):
                    if fm.groupdict().get("t%d" % n) and left:
                        row[{"to": "to_team", "from": "from_team", "nba": "nba_team"}[role]] = left.pop(0)'''

i = src.find("# each sentence with its links replaced by {P}")
j = src.find("def parse_transactions(html):")
if i < 0 or j < i or src.count(LOOP_OLD) != 1:
    sys.exit("  ingest_twoway.py not as expected — nothing written")
src = src[:i] + FORMS + src[j + len("def parse_transactions(html):"):]
src = src.replace(LOOP_OLD, LOOP_NEW)
DOLD = '''row["detail"] = "%s draft %s, round %s, pick %s" % (fm.group(4), fm.group(3), fm.group(1), fm.group(2))'''
DNEW = '''row["detail"] = "%s draft %s, round %s, pick %s" % (fm.group("dr"), fm.group("yr"), fm.group("rnd"), fm.group("pick"))'''
if src.count(DOLD) != 1:
    sys.exit("  ingest_twoway.py not as expected (draft) — nothing written")
src = src.replace(DOLD, DNEW)

OLD = '''ON CONFLICT (txn_id) DO UPDATE SET player_id = COALESCE(gl_transactions.player_id, excluded.player_id)""",
                    [tid, r["txn_date"], season_of(r["txn_date"])'''
NEW = '''ON CONFLICT (txn_id) DO UPDATE SET player_id = COALESCE(gl_transactions.player_id, excluded.player_id),
                         kind = excluded.kind, from_team = excluded.from_team, to_team = excluded.to_team,
                         nba_team = excluded.nba_team, contract = excluded.contract""",
                    [tid, r["txn_date"], season_of(r["txn_date"])'''
if src.count(OLD) != 1:
    sys.exit("  ingest_twoway.py not as expected (upsert) — nothing written")
src = src.replace(OLD, NEW)
open(PATH, "w").write(src)
print("  ingest_twoway.py patched: a blank team no longer leaves a move unrecognised")
