"""Spreadsheet to markdown: small sheets inline, large ones as CSV plus a profile."""

import csv
import re
from pathlib import Path

from openpyxl.utils import get_column_letter

# long enough for a reminder schedule or a group list in one cell, short enough
# that a pasted blob cannot fill a part on its own
MAX_CELL = 1000
MAX_INLINE_ROWS = 200
MAX_INLINE_COLS = 30
PROFILE_ROWS = 5
MAX_DISTINCT = 10
MAX_PROFILED_COLS = 3


def _clip(v) -> str:
    s = "" if v is None else str(v)
    s = " ".join(s.split())
    return (s[:MAX_CELL] + "…") if len(s) > MAX_CELL else s


def _md_table(rows: list[list[str]], first: int) -> str:
    """The rows as a table whose first column is each row's sheet row number
    (``rows[0]``, the header, is row ``first``), so a locator can name a cell
    range."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [
        [_clip(c).replace("|", "\\|") for c in r] + [""] * (width - len(r))
        for r in rows
    ]
    out = ["| (row) | " + " | ".join(rows[0]) + " |", "|" + "---|" * (width + 1)]
    out += [
        f"| {n} | " + " | ".join(r) + " |" for n, r in enumerate(rows[1:], first + 1)
    ]
    return "\n".join(out)


def _range(rows: list[list[str]], first: int) -> str:
    col = get_column_letter(max(len(r) for r in rows))
    last = first + len(rows) - 1
    return f"- range: A{first}:{col}{last} (row {first} is the header)"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "sheet"


def _profile(rows: list[list[str]], first: int) -> str:
    header, body = rows[0], rows[1:]
    lines = [
        f"- columns: {', '.join(_clip(h) or '(blank)' for h in header)}",
        f"- rows: {len(rows)}",
        _range(rows, first),
        "",
        _md_table(rows[: PROFILE_ROWS + 1], first),
    ]
    distinct_lines = []
    for i, name in enumerate(header):
        if len(distinct_lines) >= MAX_PROFILED_COLS:
            break
        values = {_clip(r[i]) for r in body if i < len(r) and _clip(r[i])}
        if 0 < len(values) <= MAX_DISTINCT:
            distinct_lines.append(f"- {_clip(name)}: {', '.join(sorted(values))}")
    if distinct_lines:
        lines += ["", "Distinct values:", *distinct_lines]
    return "\n".join(lines)


def convert_sheets(
    rows_by_sheet: dict[str, list[list[str]]], out_dir: Path, sha: str
) -> tuple[str, list[Path]]:
    """Return (markdown, csv files written into out_dir)."""
    parts: list[str] = []
    csvs: list[Path] = []
    for name, rows in rows_by_sheet.items():
        parts.append(f"## Sheet: {name}\n")
        # the table starts at its first non-blank row, its header
        top = next((i for i, r in enumerate(rows) if any(c.strip() for c in r)), None)
        if top is None:
            parts.append("(empty)\n")
            continue
        table, first = rows[top:], top + 1
        width = max(len(r) for r in table)
        if len(table) <= MAX_INLINE_ROWS and width <= MAX_INLINE_COLS:
            parts.append(f"{_range(table, first)}\n\n{_md_table(table, first)}\n")
            continue
        out_dir.mkdir(parents=True, exist_ok=True)
        slug, n = _slug(name), 1
        while (
            csv_path := out_dir / f"{sha}.{slug}{'' if n == 1 else f'-{n}'}.csv"
        ) in csvs:
            n += 1
        with open(csv_path, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)  # whole, so its line n is sheet row n
        csvs.append(csv_path)
        parts.append(
            f"Large sheet, full data in `{csv_path.name}`.\n\n"
            f"{_profile(table, first)}\n"
        )
    return "\n".join(parts), csvs
