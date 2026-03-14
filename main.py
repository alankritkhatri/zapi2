from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from simple_salesforce import Salesforce
import os
from dotenv import load_dotenv
import openai
import pandas as pd
import subprocess
import json

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

sf_connection = None

SYSTEM_PROMPT = """You are Zapi, an intelligent Salesforce assistant. Analyze the user's request and return ONLY a JSON object — no markdown, no explanation.

SUPPORTED ACTIONS:

1. QUERY - Fetch and display data
{"action":"query","soql":"SELECT ... FROM ... LIMIT 20","chart_type":"bar|pie|line|doughnut|none","chart_x":"FieldName","chart_y":"FieldName","title":"Descriptive Title"}
IMPORTANT: 'soql' MUST start with 'SELECT'. NEVER perform DELETE, INSERT, or UPDATE operations in 'soql'.

2. CREATE - Insert a new record
{"action":"create","object":"ObjectName","data":{"Field":"Value"},"title":"Created ObjectName"}

3. UPDATE - Modify existing records by condition
{"action":"update","object":"ObjectName","where":"SOQL WHERE clause (no WHERE keyword)","data":{"Field":"Value"},"title":"Updated records"}

4. DELETE - Remove a record by Id
{"action":"delete","object":"ObjectName","id":"RecordId","title":"Deleted record"}

5. DASHBOARD - Multiple data panels
{"action":"dashboard","title":"Dashboard Name","widgets":[
  {"title":"Panel Title","soql":"SELECT ...","chart_type":"bar|pie|line|doughnut|none","chart_x":"Field","chart_y":"Field"},
  {"title":"Panel 2","soql":"SELECT ...","chart_type":"none"}
]}

KEY RULES:
- Queries: LIMIT 20 by default unless user specifies
- Opportunity creates: always include StageName="Prospecting" and CloseDate 30 days from today (YYYY-MM-DD format)
- Lead creates: Company field is required
- Account creates: Name is required
- Contact creates: LastName is required
- chart_type: use "pie" or "doughnut" for category breakdowns, "bar" for comparing named items, "line" for time series, "none" for detail tables
- chart_x and chart_y must exactly match field aliases in the SELECT clause
- For GROUP BY queries use COUNT(Id) or SUM(field) with aliases
- Dashboards: create 3-4 widgets mixing charts and detail tables
- Return ONLY the JSON object

EXAMPLES:
User: "show accounts by industry"
{"action":"query","soql":"SELECT Industry, COUNT(Id) cnt FROM Account WHERE Industry != null GROUP BY Industry ORDER BY cnt DESC LIMIT 20","chart_type":"pie","chart_x":"Industry","chart_y":"cnt","title":"Accounts by Industry"}

User: "create a lead for John Doe at Acme, john@acme.com"
{"action":"create","object":"Lead","data":{"FirstName":"John","LastName":"Doe","Company":"Acme","Email":"john@acme.com"},"title":"New Lead: John Doe @ Acme"}

User: "create account TechCorp with 5M revenue in tech industry"
{"action":"create","object":"Account","data":{"Name":"TechCorp","AnnualRevenue":5000000,"Industry":"Technology"},"title":"New Account: TechCorp"}

User: "show me a sales dashboard"
{"action":"dashboard","title":"Sales Dashboard","widgets":[
  {"title":"Pipeline by Stage","soql":"SELECT StageName, COUNT(Id) cnt FROM Opportunity WHERE IsClosed=false GROUP BY StageName","chart_type":"pie","chart_x":"StageName","chart_y":"cnt"},
  {"title":"Top Accounts by Revenue","soql":"SELECT Name, AnnualRevenue FROM Account WHERE AnnualRevenue > 0 ORDER BY AnnualRevenue DESC LIMIT 10","chart_type":"bar","chart_x":"Name","chart_y":"AnnualRevenue"},
  {"title":"Recent Opportunities","soql":"SELECT Name, StageName, Amount, CloseDate FROM Opportunity ORDER BY CreatedDate DESC LIMIT 10","chart_type":"none"},
  {"title":"Leads by Status","soql":"SELECT Status, COUNT(Id) cnt FROM Lead GROUP BY Status","chart_type":"doughnut","chart_x":"Status","chart_y":"cnt"}
]}

User: "update all accounts in tech industry, set rating to Hot"
{"action":"update","object":"Account","where":"Industry = 'Technology'","data":{"Rating":"Hot"},"title":"Updated Tech Accounts"}
"""


def get_sf_auth_from_cli():
    try:
        result = subprocess.run(
            ['sf', 'org', 'display', '--json'],
            capture_output=True, text=True, shell=True
        )
        if result.returncode != 0:
            return None, None
        data = json.loads(result.stdout)
        return data['result']['accessToken'], data['result']['instanceUrl']
    except Exception as e:
        print(f"CLI Error: {e}")
        return None, None


def execute_soql(soql):
    if not soql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed for data retrieval. Use CREATE, UPDATE, or DELETE actions for modifications.")
    results = sf_connection.query_all(soql)
    records = results.get('records', [])
    for r in records:
        r.pop('attributes', None)
    return records


def make_table(records):
    if not records:
        return None, None
    df = pd.DataFrame(records)
    html = df.to_html(classes='sf-table', index=False, border=0)
    return df, html


def extract_chart_data(df, chart_x, chart_y):
    if not chart_x or not chart_y or df is None:
        return None, None
    try:
        # Case insensitive column matching
        cols_map = {c.lower(): c for c in df.columns}
        actual_x = cols_map.get(str(chart_x).lower())
        actual_y = cols_map.get(str(chart_y).lower())

        if actual_x and actual_y:
            labels = df[actual_x].head(15).astype(str).tolist()
            data = pd.to_numeric(df[actual_y].head(15), errors='coerce').fillna(0).tolist()
            return labels, data
    except Exception:
        pass
    return None, None


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    global sf_connection
    token, url = get_sf_auth_from_cli()
    if token and url and not sf_connection:
        try:
            sf_connection = Salesforce(instance_url=url, session_id=token)
            return templates.TemplateResponse("index.html", {
                "request": request, "connected": True,
                "message": "Connected via Salesforce CLI"
            })
        except Exception:
            pass
    return templates.TemplateResponse("index.html", {
        "request": request, "connected": sf_connection is not None
    })


@app.post("/connect")
async def connect(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    token: str = Form(...),
    openai_key: str = Form(...)
):
    global sf_connection
    try:
        sf_connection = Salesforce(
            username=username,
            password=password,
            security_token=token,
            domain='test' if ('.develop.' in username or '.sandbox.' in username) else None
        )
        os.environ["OPENAI_API_KEY"] = openai_key
        return templates.TemplateResponse("index.html", {
            "request": request, "connected": True, "message": "Connected!"
        })
    except Exception as e:
        try:
            sf_connection = Salesforce(
                username=username,
                password=password,
                security_token=token,
                instance_url="https://orgfarm-2e4b2bbcce-dev-ed.develop.my.salesforce.com"
            )
            os.environ["OPENAI_API_KEY"] = openai_key
            return templates.TemplateResponse("index.html", {
                "request": request, "connected": True, "message": "Connected!"
            })
        except Exception:
            return templates.TemplateResponse("index.html", {
                "request": request, "connected": False, "error": str(e)
            })


@app.post("/query")
async def query(request: Request, nl_query: str = Form(...)):
    if not sf_connection:
        return templates.TemplateResponse("index.html", {
            "request": request, "connected": False, "error": "Not connected to Salesforce"
        })

    client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    ctx = {"request": request, "connected": True, "nl_query": nl_query}

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": nl_query}
            ],
            temperature=0,
            response_format={"type": "json_object"}
        )
        intent = json.loads(response.choices[0].message.content)
        action = intent.get("action")
        ctx["ai_title"] = intent.get("title", "Result")

        # ---- QUERY ----
        if action == "query":
            soql = intent["soql"]
            records = execute_soql(soql)
            ctx["soql"] = soql
            ctx["record_count"] = len(records)

            if records:
                df, html_table = make_table(records)
                ctx["table"] = html_table
                labels, data = extract_chart_data(df, intent.get("chart_x"), intent.get("chart_y"))
                if labels and data and intent.get("chart_type", "none") != "none":
                    ctx["chart_labels"] = json.dumps(labels)
                    ctx["chart_data"] = json.dumps(data)
                    ctx["chart_type"] = intent.get("chart_type", "bar")
            else:
                ctx["message"] = "No records found for that query."

        # ---- CREATE ----
        elif action == "create":
            obj_name = intent["object"]
            data = intent["data"]
            result = getattr(sf_connection, obj_name).create(data)
            if result.get('success'):
                ctx["success_action"] = "create"
                ctx["created_object"] = obj_name
                ctx["created_data"] = data
                ctx["created_id"] = result['id']
            else:
                ctx["error"] = f"Failed to create {obj_name}: {result.get('errors')}"

        # ---- UPDATE ----
        elif action == "update":
            obj_name = intent["object"]
            where = intent.get("where", "")
            data = intent["data"]
            records = execute_soql(f"SELECT Id FROM {obj_name} WHERE {where} LIMIT 200")
            sf_obj = getattr(sf_connection, obj_name)
            updated = 0
            for rec in records:
                try:
                    sf_obj.update(rec['Id'], data)
                    updated += 1
                except Exception:
                    pass
            ctx["message"] = f"Updated {updated} {obj_name} record(s) — set {', '.join(f'{k}={v}' for k, v in data.items())}"

        # ---- DELETE ----
        elif action == "delete":
            obj_name = intent["object"]
            record_id = intent.get("id")
            if not record_id:
                ctx["error"] = "Please provide a record ID to delete (e.g. 'delete account with ID 001...')"
            else:
                getattr(sf_connection, obj_name).delete(record_id)
                ctx["message"] = f"Deleted {obj_name} record (ID: {record_id})"

        # ---- DASHBOARD ----
        elif action == "dashboard":
            widgets = []
            for w in intent.get("widgets", []):
                try:
                    records = execute_soql(w["soql"])
                    df, html_table = make_table(records)
                    labels, data = extract_chart_data(df, w.get("chart_x"), w.get("chart_y"))
                    chart_type = w.get("chart_type", "none")
                    widgets.append({
                        "title": w["title"],
                        "table": html_table,
                        "chart_type": chart_type,
                        "chart_labels": json.dumps(labels) if labels and chart_type != "none" else None,
                        "chart_data": json.dumps(data) if data and chart_type != "none" else None,
                        "record_count": len(records)
                    })
                except Exception as e:
                    widgets.append({"title": w.get("title", "Widget"), "error": str(e), "chart_type": "none"})
            ctx["dashboard"] = True
            ctx["dashboard_title"] = intent.get("title", "Dashboard")
            ctx["widgets"] = widgets

        else:
            ctx["error"] = "I couldn't understand that request. Try rephrasing — e.g. 'show all leads', 'create account Acme', or 'sales dashboard'."

    except json.JSONDecodeError:
        ctx["error"] = "AI response parsing failed. Please try again."
    except Exception as e:
        ctx["error"] = str(e)

    return templates.TemplateResponse("index.html", ctx)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
