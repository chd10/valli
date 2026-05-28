import os
import re
import logging
import warnings
from datetime import date

import openpyxl
import pandas as pd
from rapidfuzz import fuzz, process

PRICE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "price.xlsx")

logger = logging.getLogger(__name__)

_df: pd.DataFrame | None = None
_df_noprice: pd.DataFrame | None = None


def _normalize(s: str) -> str:
    return re.sub(r"[\s\-_/\\.]", "", s).lower()


def _try_parse_date(token: str) -> date | None:
    """Parse digit-only token as date. Formats: DDMMYY (6), MMYY (4), MYY (3)."""
    try:
        n = len(token)
        if n == 6:  # DDMMYY
            dd, mm, yy = int(token[:2]), int(token[2:4]), int(token[4:])
            return date(2000 + yy, mm, dd)
        elif n == 4:  # MMYY
            mm, yy = int(token[:2]), int(token[2:])
            if 1 <= mm <= 12:
                return date(2000 + yy, mm, 1)
        elif n == 3:  # MYY — single-digit month
            mm, yy = int(token[0]), int(token[1:])
            if 1 <= mm <= 9:
                return date(2000 + yy, mm, 1)
    except (ValueError, OverflowError):
        pass
    return None


def _most_recent_date(comment: str) -> date | None:
    """
    Extract the most recent date from a comment.
    Each line has format: price [name] [MMYY|DDMMYY] or [name] [MMYY].
    The first digit-only token on a line is always the USD price — skip it.
    The last digit-only token that is NOT the first token is the date.
    """
    best: date | None = None
    ceiling = date(date.today().year + 2, 1, 1)
    for line in comment.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("автор"):
            continue
        # Line must have at least one letter-containing token (name/label present)
        if not re.search(r"[A-Za-zА-Яа-яЁё]", line):
            continue
        tokens = line.split()
        if not any(re.match(r"^\d+$", t) for t in tokens):
            continue
        # If the FIRST token on the line is a digit, it's the USD price — skip it
        first_token_is_price = bool(re.match(r"^\d+$", tokens[0]))
        # Take the last digit token that is not the price (first) token
        for tok in reversed(tokens):
            if re.match(r"^\d+$", tok):
                if first_token_is_price and tok == tokens[0]:
                    break  # only one digit token and it's the price
                d = _try_parse_date(tok)
                if d is not None and d < ceiling:
                    if best is None or d > best:
                        best = d
                break
    return best


def _stale_cutoff() -> date:
    today = date.today()
    month = today.month - 3
    year = today.year
    if month <= 0:
        month += 12
        year -= 1
    return date(year, month, 1)


def _is_stale(comment: str | None) -> bool:
    if not comment:
        return True
    d = _most_recent_date(comment)
    if d is None:
        return True
    return d < _stale_cutoff()


def _format_excel_date(val) -> str | None:
    """Convert pandas date string from Excel to DD.MM.YYYY, or None if empty."""
    s = str(val).strip() if val is not None else ""
    if not s or s.lower() in ("nan", "none", "nat", ""):
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(3)}.{m.group(2)}.{m.group(1)}"
    if re.match(r"^\d{2}\.\d{2}\.\d{4}$", s):
        return s
    return s


def _load_comments() -> list[str | None]:
    """Read comments from column K (index 10) starting from row 3."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        wb = openpyxl.load_workbook(PRICE_PATH)
    ws = wb["Глав"]
    result = []
    for row in ws.iter_rows(min_row=3, max_col=11):
        cell = row[10]  # column K
        result.append(cell.comment.text if cell.comment else None)
    wb.close()
    return result


def _has_nonzero_price(v) -> bool:
    """True if v is a parseable number strictly greater than 0."""
    try:
        return float(str(v).replace(" ", "").replace(",", ".")) > 0
    except Exception:
        return False


def _load() -> pd.DataFrame:
    global _df, _df_noprice
    if _df is None:
        # A(0)=кондиция, B(1)=EoL дата, C(2)=артикул, D(3)=цена руб, M(12)=Обновлено
        df = pd.read_excel(
            PRICE_PATH,
            sheet_name="Глав",
            header=None,
            skiprows=2,
            usecols=[0, 1, 2, 3, 12],
            dtype=str,
        )
        df.columns = ["condition", "eol", "article", "price", "updated"]

        comments = _load_comments()
        n = len(df)
        df["comment"] = (comments + [None] * n)[:n]

        df = df.dropna(subset=["article"])
        df["article"] = df["article"].str.strip()
        df = df[df["article"] != ""]
        df["condition"] = df["condition"].fillna("").str.strip()
        # If col A contains a number (e.g. 80000) — treat as empty; real condition
        # values are text labels like "used", "ref", "likenew", "местно", etc.
        df.loc[df["condition"].apply(_is_numeric), "condition"] = ""
        df["price"] = df["price"].fillna("")
        df["eol"] = df["eol"].apply(_format_excel_date).fillna("")
        df["updated"] = df["updated"].apply(_format_excel_date).fillna("")

        df["article_lower"] = df["article"].str.lower()
        df["article_norm"] = df["article_lower"].apply(_normalize)
        df["stale"] = df["comment"].apply(_is_stale)

        has_price = df["price"].apply(_has_nonzero_price)
        _df_noprice = df[~has_price].reset_index(drop=True)
        _df = df[has_price].reset_index(drop=True)
    return _df


def _load_noprice() -> pd.DataFrame:
    _load()
    return _df_noprice


def reload():
    global _df, _df_noprice
    _df = None
    _df_noprice = None
    _load()


def search_containing(query: str, limit: int = 20) -> list[dict]:
    """All priced articles whose normalized name contains the normalized query as a substring."""
    df = _load()
    q_norm = _normalize(query)
    if not q_norm:
        return []
    matched = df[df["article_norm"].str.contains(q_norm, regex=False)].head(limit)
    result: list[dict] = []
    seen: set[str] = set()
    for _, row in matched.iterrows():
        art = row["article"]
        if art not in seen:
            seen.add(art)
            result.append({
                "article": art,
                "condition": _format_condition_short(row["condition"]),
            })
    logger.info("search_containing(%r) → %d candidates: %s",
                query, len(result), [r["article"] for r in result])
    return result


def _is_numeric(s: str) -> bool:
    try:
        float(s.replace(" ", "").replace(",", "."))
        return True
    except ValueError:
        return False


def _format_condition(raw: str) -> str:
    if not raw or _is_numeric(raw):
        return "новое оборудование"
    return raw


_CONDITION_LABELS: dict[str, str] = {
    "new": "новое",
    "likenew": "как новое",
    "like new": "как новое",
    "ref": "восстановленное",
    "refurbished": "восстановленное",
    "used": "б/у",
}

_CONDITION_PRIORITY: dict[str, int] = {
    "":          0,  # empty → новое оборудование
    "new":       0,
    "likenew":   1,
    "like new":  1,
    "ref":       2,
    "refurbished": 2,
    "used":      3,
}


def _condition_sort_key(raw: str) -> int:
    if not raw or _is_numeric(raw):
        return 0
    return _CONDITION_PRIORITY.get(raw.lower().strip(), 4)


def _format_condition_short(raw: str) -> str:
    if not raw or _is_numeric(raw):
        return "новое"
    return _CONDITION_LABELS.get(raw.lower().strip(), raw)


def _format_price(raw: str) -> str:
    try:
        val = float(str(raw).replace(" ", "").replace(",", "."))
        return f"{val:,.0f}".replace(",", " ") + " руб. с НДС"
    except Exception:
        return str(raw)


_FUZZY_LIMIT = 3


def search(query: str, score_cutoff: int = 65) -> list[dict]:
    df = _load()
    q = query.strip()
    q_lower = q.lower()
    q_norm = _normalize(q)
    logger.info("search(%r)", query)

    seen: set[int] = set()

    def make_row(idx: int, score: int, fuzzy: bool) -> dict:
        row = df.iloc[idx]
        return {
            "article": row["article"],
            "condition": _format_condition(row["condition"]),
            "price": _format_price(row["price"]),
            "score": score,
            "stale": bool(row["stale"]),
            "fuzzy": fuzzy,
            "is_used": row["condition"].lower().strip() == "used",
            "eol": row["eol"],
            "updated": row["updated"],
            "_sort_key": _condition_sort_key(row["condition"]),
        }

    # Exact match (case-insensitive)
    exact: list[dict] = []
    for idx in df[df["article_lower"] == q_lower].index:
        i = int(idx)
        if i not in seen:
            seen.add(i)
            exact.append(make_row(i, 100, False))

    # Normalized exact (strips dashes, spaces, dots)
    if q_norm:
        for idx in df[df["article_norm"] == q_norm].index:
            i = int(idx)
            if i not in seen:
                seen.add(i)
                exact.append(make_row(i, 99, False))

    # Exact match found — return only those, no fuzzy
    if exact:
        exact.sort(key=lambda r: r.pop("_sort_key"))
        logger.info("search(%r) → exact match: %s", query, [r["article"] for r in exact])
        return exact

    # Article exists but price is empty/zero — report as no_price
    np = _load_noprice()
    for idx in np[np["article_lower"] == q_lower].index:
        row = np.iloc[int(idx)]
        logger.info("search(%r) → no_price: %s", query, row["article"])
        return [{"article": row["article"], "no_price": True, "fuzzy": False}]
    if q_norm:
        for idx in np[np["article_norm"] == q_norm].index:
            row = np.iloc[int(idx)]
            logger.info("search(%r) → no_price (norm): %s", query, row["article"])
            return [{"article": row["article"], "no_price": True, "fuzzy": False}]

    # No exact match — fuzzy with hard limit of 3
    fuzzy: list[dict] = []

    for _, score, idx in process.extract(
        q_lower, df["article_lower"].tolist(),
        scorer=fuzz.WRatio,
        limit=_FUZZY_LIMIT * 3,
        score_cutoff=score_cutoff,
    ):
        if len(fuzzy) >= _FUZZY_LIMIT:
            break
        i = int(idx)
        if i not in seen:
            seen.add(i)
            fuzzy.append(make_row(i, int(score), True))

    if len(fuzzy) < _FUZZY_LIMIT and q_norm:
        for _, score, idx in process.extract(
            q_norm, df["article_norm"].tolist(),
            scorer=fuzz.WRatio,
            limit=_FUZZY_LIMIT * 3,
            score_cutoff=score_cutoff,
        ):
            if len(fuzzy) >= _FUZZY_LIMIT:
                break
            i = int(idx)
            if i not in seen:
                seen.add(i)
                fuzzy.append(make_row(i, int(score), True))

    if fuzzy:
        logger.info("search(%r) → fuzzy: %s", query, [r["article"] for r in fuzzy])
    else:
        logger.info("search(%r) → no results", query)
    return fuzzy
