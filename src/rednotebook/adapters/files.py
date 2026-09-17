import csv
import json
from pathlib import Path

from rednotebook.errors import DomainError

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 5000
JSON_COLUMNS = {"metrics", "sampling", "coverage"}
BOOL_COLUMNS = {"synthetic", "is_reply", "is_author_reply"}


def read_rows(path: Path):
    if path.stat().st_size > MAX_BYTES:
        raise DomainError("input_file_too_large")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise DomainError("input_file_too_large")
    text = raw.decode("utf-8-sig")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise DomainError("invalid_json_document") from None
        if not isinstance(data, list):
            raise DomainError("json_root_must_be_array")
        rows = [(index + 1, item, None) for index, item in enumerate(data)]
    elif path.suffix.lower() == ".csv":
        # Embedded objects are JSON cells; top-level headers match EvidenceInput.
        import io

        csv.field_size_limit(MAX_BYTES)
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames):
            raise DomainError("invalid_csv_headers")
        rows = []
        try:
            for index, item in enumerate(reader, start=2):
                error = None
                converted = {}
                if None in item or any(v is None for v in item.values()):
                    rows.append((index, {}, [{"field": "row", "code": "csv_column_count"}]))
                    continue
                for key, value in item.items():
                    try:
                        if value == "":
                            continue  # omitted optional field; required fields fail validation
                        if key in JSON_COLUMNS:
                            converted[key] = json.loads(value)
                        elif key in BOOL_COLUMNS:
                            if value not in {"true", "false"}:
                                raise ValueError()
                            converted[key] = value == "true"
                        elif key == "followers":
                            converted[key] = int(value)
                        else:
                            converted[key] = value
                    except (ValueError, TypeError):
                        error = [{"field": key, "code": "invalid_csv_cell"}]
                        break
                rows.append((index, converted, error))
        except csv.Error:
            raise DomainError("invalid_csv_document") from None
    else:
        raise DomainError("supported_formats_are_json_and_csv")
    if len(rows) > MAX_ROWS:
        raise DomainError("input_row_budget_exceeded")
    return raw, rows
