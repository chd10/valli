import os
import re
import logging

import pandas as pd

STOCK_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "warehouse", "stock_ediscom.xlsx")

logger = logging.getLogger(__name__)

_df = None


def _clean_article(raw: str) -> str:
    """Strip annotations in parentheses (e.g. '(Ch)', '(ch)') and whitespace."""
    return re.sub(r"\s*\([^)]*\)", "", str(raw)).strip()


def _parse_qty(v) -> int:
    try:
        return int(float(str(v).replace(" ", "").replace(",", ".")))
    except Exception:
        return 0


def _load():
    global _df
    if _df is not None:
        return _df
    if not os.path.exists(STOCK_PATH):
        logger.warning("Файл склада не найден: %s", STOCK_PATH)
        return None
    try:
        # Sheet "Лист_1", data starts at row 10 (skiprows=9 skips rows 1-9)
        # col A (0) = article, col K (10) = qty
        df = pd.read_excel(
            STOCK_PATH,
            sheet_name="Лист_1",
            header=None,
            skiprows=9,
            usecols=[0, 10],
            dtype=str,
        )
        df.columns = ["article_raw", "qty"]
        df = df.dropna(subset=["article_raw"])
        df["article_raw"] = df["article_raw"].astype(str).str.strip()
        df = df[df["article_raw"].str.len() > 0]
        df = df[df["article_raw"] != "nan"]
        df["article_clean"] = df["article_raw"].apply(_clean_article)
        df["article_lower"] = df["article_clean"].str.lower()
        df["qty"] = df["qty"].apply(_parse_qty)
        df = df[df["qty"] > 0].reset_index(drop=True)
        _df = df
        logger.info("Склад загружен: %d позиций", len(_df))
    except Exception:
        logger.exception("Ошибка загрузки файла склада")
        return None
    return _df


def reload() -> None:
    global _df
    _df = None
    _load()


def search_stock(query: str) -> dict | None:
    """Return {article, qty} if article found in stock with qty > 0, else None."""
    df = _load()
    if df is None or df.empty:
        return None

    q_clean = _clean_article(query).lower()

    # Exact match on cleaned article
    exact = df[df["article_lower"] == q_clean]
    if not exact.empty:
        row = exact.iloc[0]
        logger.info("stock_search(%r) → exact: %s (%d шт.)", query, row["article_clean"], row["qty"])
        return {"article": row["article_clean"], "qty": int(row["qty"])}

    # Partial match (query is substring of stock article or vice versa)
    if len(q_clean) >= 4:
        partial = df[df["article_lower"].str.contains(q_clean, regex=False)]
        if not partial.empty:
            row = partial.iloc[0]
            logger.info("stock_search(%r) → partial: %s (%d шт.)", query, row["article_clean"], row["qty"])
            return {"article": row["article_clean"], "qty": int(row["qty"])}

    logger.info("stock_search(%r) → не найдено", query)
    return None
