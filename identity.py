#!/usr/bin/env python3
"""
identity.py — Box and One name normalization and match scoring.

Two jobs:
  1. normalize_name()  — turn any source's name string into stable keys
  2. score_match()     — score a staged observation against a registry candidate

Design rule that drives everything: SUFFIXES ARE NEVER STRIPPED SILENTLY.
Gary Payton and Gary Payton II are different people. So are Tim Hardaway and
Tim Hardaway Jr. Collapsing them into one key merges fathers with sons.

So we keep two keys:
  name_base  — suffix removed, used ONLY to generate candidates
  name_key   — suffix preserved, used for equality

Standard library only. No dependencies.
"""

import re
import unicodedata
from datetime import date
from difflib import SequenceMatcher

# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

# Characters that do not decompose under NFKD and need an explicit map.
_CHAR_MAP = {
    "đ": "d", "Đ": "D",   # Đorđe Petrović
    "ð": "d", "Ð": "D",
    "ø": "o", "Ø": "O",   # Scandinavian
    "ł": "l", "Ł": "L",   # Polish
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ß": "ss",
    "þ": "th", "Þ": "TH",
    "ı": "i",             # Turkish dotless i
}

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}

# Generational rank. Used to detect father/son conflicts.
_SUFFIX_RANK = {None: 0, "sr": 1, "jr": 2, "ii": 2, "iii": 3, "iv": 4, "v": 5}


def strip_accents(s: str) -> str:
    """Jokić -> Jokic, Dončić -> Doncic, Đorđe -> Dorde."""
    for src, dst in _CHAR_MAP.items():
        s = s.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", s)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def split_suffix(s: str):
    """Return (name_without_suffix, suffix_or_None). Suffix lowercased."""
    tokens = s.split()
    if len(tokens) < 2:
        return s, None
    tail = tokens[-1].lower().rstrip(".")
    if tail in SUFFIXES:
        return " ".join(tokens[:-1]), tail
    return s, None


def normalize_name(raw: str) -> dict:
    """
    Returns:
      name_key   suffix preserved   'gary payton ii'
      name_base  suffix removed     'gary payton'
      suffix     'ii' or None
      first/last best-effort parts
    """
    if raw is None:
        raise ValueError("normalize_name received None")

    s = raw.strip()

    # "Jokic, Nikola" -> "Nikola Jokic"
    if "," in s:
        head, _, tail = s.partition(",")
        tail_clean = tail.strip().rstrip(".")
        # only flip if the tail isn't itself a suffix ("Payton, II")
        if tail_clean.lower() not in SUFFIXES and tail.strip():
            s = f"{tail.strip()} {head.strip()}"
        else:
            s = f"{head.strip()} {tail.strip()}"

    s = strip_accents(s)
    s = s.lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[’'`]", "", s)         # O'Neal -> oneal
    s = re.sub(r"[-–—]", " ", s)        # Gilgeous-Alexander -> gilgeous alexander
    s = re.sub(r"[^a-z0-9. ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()

    base, suffix = split_suffix(s)
    base = re.sub(r"\.", "", base).strip()
    base = re.sub(r"\s+", " ", base)

    name_key = f"{base} {suffix}".strip() if suffix else base

    parts = base.split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""

    return {
        "name_key": name_key,
        "name_base": base,
        "suffix": suffix,
        "first": first,
        "last": last,
    }


def name_similarity(a: str, b: str) -> float:
    """0.0-1.0 on already-normalized base names."""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    ratio = SequenceMatcher(None, a, b).ratio()

    # Token-level agreement catches middle names and reordering.
    ta, tb = set(a.split()), set(b.split())
    if ta and tb:
        jaccard = len(ta & tb) / len(ta | tb)
        ratio = max(ratio, 0.55 * ratio + 0.45 * jaccard)
    return round(ratio, 4)


# ---------------------------------------------------------------------------
# Suffix compatibility
# ---------------------------------------------------------------------------

def suffix_relation(a, b) -> str:
    """
    'same'       identical suffixes, or both absent
    'ambiguous'  one present, one absent — sources disagree OR father/son
    'conflict'   both present and different — different people
    """
    if a == b:
        return "same"
    if a is None or b is None:
        return "ambiguous"
    if _SUFFIX_RANK.get(a, 9) != _SUFFIX_RANK.get(b, 9):
        return "conflict"
    return "same"   # 'jr' and 'ii' both rank 2


# ---------------------------------------------------------------------------
# Match scoring
# ---------------------------------------------------------------------------

WEIGHTS = {
    "name": 0.50,
    "birthdate": 0.30,
    "draft_year": 0.10,
    "school": 0.07,
    "height": 0.03,
}


def _cmp_birthdate(a, b):
    if a is None or b is None:
        return None
    if a == b:
        return 1.0
    delta = abs((a - b).days)
    if delta <= 1:
        return 0.90          # timezone / off-by-one in a source
    if a.month == b.month and a.day == b.day:
        return 0.35          # right day, wrong year — classic age-fraud shape
    if delta <= 400:
        return 0.15          # near miss; likely one source is wrong
    return 0.0


def _cmp_school(a, b):
    if not a or not b:
        return None
    na = re.sub(r"[^a-z ]", "", strip_accents(a).lower()).strip()
    nb = re.sub(r"[^a-z ]", "", strip_accents(b).lower()).strip()
    if not na or not nb:
        return None
    if na == nb:
        return 1.0
    return round(SequenceMatcher(None, na, nb).ratio(), 4)


def _cmp_height(a, b):
    if a is None or b is None:
        return None
    d = abs(a - b)
    if d <= 1:
        return 1.0
    if d <= 2:
        return 0.6
    if d <= 4:
        return 0.2
    return 0.0


def score_match(observed: dict, candidate: dict) -> dict:
    """
    observed  — a staging_players row (raw_name, raw_birthdate, raw_draft_year,
                raw_school, raw_height_in)
    candidate — a players row (full_name, birthdate, birthdate_status,
                draft_year, college, height_in)

    Returns score, decision, and human-readable reasons.
    Decisions: 'auto_link' | 'propose' | 'review' | 'block'
    """
    obs_n = normalize_name(observed["raw_name"])
    can_n = normalize_name(candidate["full_name"])

    reasons = []
    sim = name_similarity(obs_n["name_base"], can_n["name_base"])
    reasons.append(f"name={sim:.2f}")

    rel = suffix_relation(obs_n["suffix"], can_n["suffix"])
    if rel != "same":
        reasons.append(f"suffix={rel}({obs_n['suffix']}/{can_n['suffix']})")

    # Hard block: different generational suffixes are different humans.
    if rel == "conflict":
        return {
            "score": 0.0,
            "decision": "block",
            "reasons": ";".join(reasons + ["suffix_conflict"]),
            "name_similarity": sim,
        }

    parts, total_w = {}, 0.0

    parts["name"] = sim
    total_w += WEIGHTS["name"]

    bd = _cmp_birthdate(observed.get("raw_birthdate"), candidate.get("birthdate"))
    if bd is not None:
        parts["birthdate"] = bd
        total_w += WEIGHTS["birthdate"]
        reasons.append(f"bday={bd:.2f}")
    else:
        reasons.append("bday=missing")

    if observed.get("raw_draft_year") and candidate.get("draft_year"):
        dy = 1.0 if observed["raw_draft_year"] == candidate["draft_year"] else 0.0
        parts["draft_year"] = dy
        total_w += WEIGHTS["draft_year"]
        reasons.append(f"draft={dy:.0f}")

    sc = _cmp_school(observed.get("raw_school"), candidate.get("college"))
    if sc is not None:
        parts["school"] = sc
        total_w += WEIGHTS["school"]
        reasons.append(f"school={sc:.2f}")

    ht = _cmp_height(observed.get("raw_height_in"), candidate.get("height_in"))
    if ht is not None:
        parts["height"] = ht
        total_w += WEIGHTS["height"]
        reasons.append(f"height={ht:.2f}")

    score = sum(WEIGHTS[k] * v for k, v in parts.items()) / total_w if total_w else 0.0
    score = round(score, 4)

    bd_confirmed = candidate.get("birthdate_status") in ("confirmed", "admin_verified")
    bd_exact = parts.get("birthdate", 0) >= 0.90

    # A birthdate that actively disagrees kills the match regardless of name.
    if parts.get("birthdate") == 0.0:
        decision = "review"
        reasons.append("bday_conflict")
    elif rel == "ambiguous":
        # Never auto-resolve a possible father/son. Human looks at it.
        decision = "review"
        reasons.append("suffix_needs_review")
    elif sim >= 0.995 and bd_exact and bd_confirmed:
        decision = "auto_link"
    elif score >= 0.90 and bd_exact:
        decision = "propose"
    elif score >= 0.80:
        decision = "review"
    else:
        decision = "review"

    return {
        "score": score,
        "decision": decision,
        "reasons": ";".join(reasons),
        "name_similarity": sim,
    }


def rank_candidates(observed: dict, candidates: list, limit: int = 5) -> list:
    """Score all candidates, drop blocks, return best first."""
    scored = []
    for c in candidates:
        r = score_match(observed, c)
        if r["decision"] == "block":
            continue
        scored.append({**r, "player_id": c.get("player_id"),
                       "full_name": c.get("full_name")})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]
