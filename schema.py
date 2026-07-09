from __future__ import annotations
import json
from pathlib import Path
from typing import Optional
from pydantic import BaseModel


class ColumnSchema(BaseModel):
    dtype: str  # "int", "float", "str", "date"
    nullable: bool = False
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    allowed_values: Optional[list[str]] = None
    pattern: Optional[str] = None  # regex


class DataSchema(BaseModel):
    name: str
    columns: dict[str, ColumnSchema]

    @classmethod
    def from_json_file(cls, path: str) -> "DataSchema":
        """Load a schema from a JSON file. See schema/employees.json for the format."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.model_validate(data)

    def to_description(self) -> str:
        """Returns a plain-English description Claude can read."""
        lines = [f"Schema: {self.name}", ""]
        for col, spec in self.columns.items():
            parts = [f"  {col}: {spec.dtype}"]
            if not spec.nullable:
                parts.append("required (no nulls)")
            if spec.min_value is not None:
                parts.append(f"min={spec.min_value}")
            if spec.max_value is not None:
                parts.append(f"max={spec.max_value}")
            if spec.allowed_values:
                parts.append(f"one of {spec.allowed_values}")
            if spec.pattern:
                parts.append(f"pattern={spec.pattern}")
            lines.append(", ".join(parts))
        return "\n".join(lines)


EMPLOYEE_SCHEMA = DataSchema(
    name="employees",
    columns={
        "id": ColumnSchema(dtype="int", nullable=False),
        "name": ColumnSchema(dtype="str", nullable=False),
        "email": ColumnSchema(dtype="str", nullable=False, pattern=r".+@.+\..+"),
        "age": ColumnSchema(dtype="int", nullable=False, min_value=18, max_value=100),
        "salary": ColumnSchema(dtype="float", nullable=False, min_value=0),
        "department": ColumnSchema(
            dtype="str",
            nullable=False,
            allowed_values=["Engineering", "Sales", "Marketing", "HR", "Finance"],
        ),
        "start_date": ColumnSchema(dtype="date", nullable=False),
    },
)
