import json
from collections.abc import Mapping, Sequence
from typing import Any

_COLUMN_GAP = "  "


def emit(rows: Sequence[Mapping[str, Any]], as_json: bool, *, columns: Sequence[str]) -> None:
    if as_json:
        print(json.dumps(list(rows)))
        return
    table = [list(columns), *([_cell(row.get(column)) for column in columns] for row in rows)]
    widths = [max(len(line[index]) for line in table) for index in range(len(columns))]
    for line in table:
        print(_COLUMN_GAP.join(cell.ljust(width) for cell, width in zip(line, widths, strict=True)).rstrip())


def emit_record(record: Mapping[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(record))
        return
    for key, value in record.items():
        print(f"{key}: {_cell(value)}")


def emit_text(text: str, value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value))
    elif text:
        print(text)


def _cell(value: Any) -> str:
    if value is None or value == []:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ", ".join(_cell(item) for item in value)
    return " ".join(str(value).splitlines())
