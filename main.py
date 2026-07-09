"""
main.py — Entry point.

Usage:
    python main.py <csv_path> <schema_json_path>

Examples:
    python main.py data/dirty_employees.csv schema/employees.json
    python main.py data/resumes.csv schema/resumes.json

The cleaned CSV is written alongside the input file as <name>_clean.csv
"""
import os
import sys
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
from dotenv import load_dotenv
import pandas as pd

from schema import DataSchema
from agent import run_agent

load_dotenv()

if not os.getenv("ANTHROPIC_API_KEY"):
    raise SystemExit("ERROR: Set ANTHROPIC_API_KEY in your .env file first.")

if len(sys.argv) != 3:
    raise SystemExit(
        "Usage: python main.py <csv_path> <schema_json_path>\n"
        "Example: python main.py data/dirty_employees.csv schema/employees.json"
    )

csv_path    = sys.argv[1]
schema_path = sys.argv[2]

if not Path(csv_path).exists():
    raise SystemExit(f"ERROR: CSV file not found: {csv_path}")
if not Path(schema_path).exists():
    raise SystemExit(f"ERROR: Schema file not found: {schema_path}")

# Derive output path: data/dirty_employees.csv → data/dirty_employees_clean.csv
input_path  = Path(csv_path)
output_path = input_path.with_name(input_path.stem + "_clean.csv")

print(f"Input:  {csv_path}")
print(f"Schema: {schema_path}")
print(f"Output: {output_path}\n")

df     = pd.read_csv(csv_path)
schema = DataSchema.from_json_file(schema_path)

print(f"Rows loaded: {len(df)}")
print(f"Schema: {schema.name} ({len(schema.columns)} columns)\n")

clean_df = run_agent(df, schema, output_path=str(output_path))

print(f"\nFinal row count: {len(clean_df)}")
print(clean_df.to_string())
