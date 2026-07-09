# Data Cleaning Agent

An AI-powered data quality tool that inspects, cleans, and reports on CSV files — field by field, row by row.

Built with **Python**, **FastAPI**, **Claude AI (Haiku)**, and **pandas**.

![Home page showing three mode cards: Audit Only, Clean + Report, Strict Clean](https://placeholder.com/screenshot)

---

## What it does

Upload a CSV, describe your data rules in plain English (or write a JSON schema), and an AI agent loops through your data: inspecting columns, identifying violations, applying safe fixes, and flagging everything it can't resolve — without silently deleting a single row.

### Three modes

| Mode | Behaviour |
|---|---|
| **Audit Only** | Validates every column and returns an error report. Your data is never changed. |
| **Clean + Report** | Auto-fixes safe errors (typos, type coercion, date formats). Flags the rest with an `_errors` column. All rows preserved. |
| **Strict Clean** | Same fixes as Clean, but rows that can't be fully corrected are removed. Output CSV is 100% valid. |

### What it catches

- Typos and wrong casing in categorical columns (`"Electronis"` → `"Electronics"`)
- Numbers stored as text (`"sixty five"` → flagged)
- Values outside allowed range (`stock_qty: -10` → flagged)
- Invalid email/pattern formats
- Missing required fields
- Wrong date formats (`15/07/2024` → converted)
- Out-of-range values (`rating: 6.2` when max is 5 → flagged)

---

## Tech stack

- **Backend:** FastAPI + Python
- **AI agent:** Anthropic Claude Haiku via tool use / function calling
- **Data:** pandas
- **Frontend:** Vanilla HTML/CSS/JS (no framework)
- **Schema:** Pydantic

---

## Getting started

### 1. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/schema-agent.git
cd schema-agent
```

### 2. Add your API key

```bash
cp .env.example .env
# Edit .env and paste your Anthropic API key
# Get one at: https://console.anthropic.com
```

### 3. Run the server

Requires [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv run --with fastapi --with "uvicorn[standard]" --with python-multipart \
       --with aiofiles --with anthropic --with pandas \
       --with pydantic --with python-dotenv \
       uvicorn app:app --reload --port 8000
```

Open [http://localhost:8000](http://localhost:8000).

### 4. (Optional) Run via CLI

```bash
uv run --with anthropic --with pandas --with pydantic --with python-dotenv \
       main.py data/dirty_employees.csv schema/employees.json
```

---

## Project structure

```
schema-agent/
├── app.py              # FastAPI server (routes, SSE streaming, job management)
├── agent.py            # Agentic loop — Claude tool use, mode-aware system prompts
├── tools.py            # pandas operations exposed as Claude tools
├── schema.py           # Pydantic schema models
├── schema_infer.py     # Natural language → schema JSON (single Claude call)
├── main.py             # CLI entry point
├── static/
│   ├── home.html       # Landing page
│   └── index.html      # Agent UI (upload, NL rules, real-time log, download)
├── schema/
│   ├── employees.json  # Example schema
│   └── products.json   # Example schema
└── data/
    ├── dirty_employees.csv
    └── dirty_products.csv
```

---

## How the agent works

1. Claude receives the schema description and a set of tool definitions (JSON)
2. Claude calls `inspect_data` → `validate_column` for each column → fix or mark tools
3. Each tool call runs a real Python/pandas function and returns the result
4. Claude reasons over the result and decides the next tool to call
5. Loop continues until all columns report zero violations, then `save_with_report` is called
6. Output: annotated CSV + error report CSV

The agent uses **prompt caching** on the system prompt and tool definitions to reduce cost across the 20–30 iterations a typical run takes.

---

## Schema format

```json
{
  "name": "my_dataset",
  "columns": {
    "email":  { "dtype": "str",   "nullable": false, "pattern": ".+@.+\\..+" },
    "age":    { "dtype": "int",   "nullable": false, "min_value": 18, "max_value": 100 },
    "status": { "dtype": "str",   "nullable": false, "allowed_values": ["Active", "Inactive"] },
    "score":  { "dtype": "float", "nullable": true,  "min_value": 0, "max_value": 1 }
  }
}
```

Supported dtypes: `int`, `float`, `str`, `date`

---

## License

MIT
