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
- Custom Salesforce objects MUST have '__c' suffix (e.g. MyObject__c). Standard objects do NOT (Account, Contact, Opportunity, Lead, Case, etc.)
- NEVER invent non-existent objects like 'DataQualityIssues'. For data quality analysis, query standard objects: missing fields on Account/Contact, stale Opportunities, etc.
- Queries: LIMIT 20 by default unless user specifies
- Opportunity creates: always include StageName="Prospecting" and CloseDate 30 days from today (YYYY-MM-DD format)
- Lead creates: Company field is required
- Account creates: Name is required
- Contact creates: LastName is required
- chart_type: use "pie" or "doughnut" for category breakdowns, "bar" for comparing named items, "line" for time series, "none" for detail tables
- chart_x and chart_y must exactly match field aliases in the SELECT clause
- For GROUP BY queries use COUNT(Id) or SUM(field) with aliases
- IMPORTANT: In 'ORDER BY', do NOT use the alias. Repeat the aggregate function. (e.g., ORDER BY COUNT(Id) DESC, not ORDER BY cnt DESC)
- Dashboards: create 3-4 widgets mixing charts and detail tables
- Return ONLY the JSON object

EXAMPLES:
User: "show accounts by industry"
{"action":"query","soql":"SELECT Industry, COUNT(Id) cnt FROM Account WHERE Industry != null GROUP BY Industry ORDER BY COUNT(Id) DESC LIMIT 20","chart_type":"pie","chart_x":"Industry","chart_y":"cnt","title":"Accounts by Industry"}

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

User: "show support dashboard"
{"action":"dashboard","title":"Support Dashboard","widgets":[
  {"title":"Cases by Priority","soql":"SELECT Priority, COUNT(Id) cnt FROM Case GROUP BY Priority","chart_type":"pie","chart_x":"Priority","chart_y":"cnt"},
  {"title":"Cases by Status","soql":"SELECT Status, COUNT(Id) cnt FROM Case GROUP BY Status","chart_type":"bar","chart_x":"Status","chart_y":"cnt"},
  {"title":"Open Cases","soql":"SELECT CaseNumber, Subject, Priority, Status FROM Case WHERE IsClosed=false ORDER BY CreatedDate DESC LIMIT 10","chart_type":"none"}
]}

User: "show marketing dashboard"
{"action":"dashboard","title":"Marketing Dashboard","widgets":[
  {"title":"Leads by Source","soql":"SELECT LeadSource, COUNT(Id) cnt FROM Lead GROUP BY LeadSource","chart_type":"doughnut","chart_x":"LeadSource","chart_y":"cnt"},
  {"title":"Leads by Status","soql":"SELECT Status, COUNT(Id) cnt FROM Lead GROUP BY Status","chart_type":"bar","chart_x":"Status","chart_y":"cnt"},
  {"title":"Recent Leads","soql":"SELECT Name, Company, LeadSource, Status FROM Lead ORDER BY CreatedDate DESC LIMIT 10","chart_type":"none"}
]}

User: "show revenue dashboard"
{"action":"dashboard","title":"Revenue Dashboard","widgets":[
  {"title":"Pipeline Amount by Stage","soql":"SELECT StageName, SUM(Amount) total FROM Opportunity WHERE IsClosed=false GROUP BY StageName ORDER BY SUM(Amount) DESC","chart_type":"bar","chart_x":"StageName","chart_y":"total"},
  {"title":"Revenue by Type","soql":"SELECT Type, SUM(Amount) total FROM Opportunity WHERE StageName='Closed Won' GROUP BY Type","chart_type":"pie","chart_x":"Type","chart_y":"total"},
  {"title":"Top Opportunities","soql":"SELECT Name, Amount, StageName, CloseDate FROM Opportunity WHERE IsClosed=false ORDER BY Amount DESC LIMIT 10","chart_type":"none"}
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
    import re
    if not soql.strip().upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed for data retrieval. Use CREATE, UPDATE, or DELETE actions for modifications.")
    try:
        results = sf_connection.query_all(soql)
    except Exception as e:
        # If the object is not found and doesn't already end with __c, retry with __c appended
        err = str(e)
        if 'INVALID_TYPE' in err and '__c' not in soql:
            # Extract object name from FROM clause and append __c
            fixed = re.sub(
                r'\bFROM\s+(\w+)(?!__c)\b',
                lambda m: f"FROM {m.group(1)}__c",
                soql, flags=re.IGNORECASE
            )
            results = sf_connection.query_all(fixed)
        else:
            raise
    records = results.get('records', [])
    for r in records:
        r.pop('attributes', None)
    return records


def check_data_quality():
    issues = []
    
    # Check 1: Accounts missing Industry
    try:
        res = execute_soql("SELECT Id, Name FROM Account WHERE Industry = NULL LIMIT 50")
        for r in res:
            issues.append({"Record": r['Name'], "Type": "Account: Missing Industry", "Recommend": "Enrich data"})
    except Exception: pass

    # Check 2: Contacts missing Email
    try:
        res = execute_soql("SELECT Id, Name FROM Contact WHERE Email = NULL LIMIT 50")
        for r in res:
            issues.append({"Record": r['Name'], "Type": "Contact: Missing Email", "Recommend": "Call/Research"})
    except Exception: pass

    # Check 3: Stale Opportunities
    try:
        res = execute_soql("SELECT Id, Name FROM Opportunity WHERE IsClosed = false AND CloseDate < TODAY LIMIT 50")
        for r in res:
            issues.append({"Record": r['Name'], "Type": "Opportunity: Stale", "Recommend": "Update/Close"})
    except Exception: pass

    if not issues:
        return [], [], []

    df = pd.DataFrame(issues)
    
    # Aggregation for Chart
    counts = df['Type'].value_counts()
    labels = counts.index.tolist()
    data = counts.values.tolist()
    
    # HTML Table
    _, html_table = make_table(issues)
    
    return labels, data, html_table


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
            
    # Try connecting via Environment Variables
    if not sf_connection:
        try:
            sf_user = os.getenv("SALESFORCE_USERNAME")
            sf_pwd = os.getenv("SALESFORCE_PASSWORD")
            sf_token = os.getenv("SALESFORCE_SECURITY_TOKEN")
            # If token is None, use empty string
            if sf_token is None:
                sf_token = ""
                
            if sf_user and sf_pwd:
                domain = 'test' if (sf_user and ('.develop.' in sf_user or '.sandbox.' in sf_user)) else None
                sf_connection = Salesforce(username=sf_user, password=sf_pwd, security_token=sf_token, domain=domain)
                return templates.TemplateResponse("index.html", {
                    "request": request, "connected": True,
                    "message": "Connected via Environment Variables"
                })
        except Exception as e:
            print(f"Environment connection failed: {str(e)}")

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

    # ---- HARDCODED: Data Quality Report ----
    _q = nl_query.lower()
    if "data quality" in _q or "quality report" in _q or "quality analysis" in _q or "quality issues" in _q:
        labels, data, html_table = check_data_quality()
        if not labels:
             return templates.TemplateResponse("index.html", {
                "request": request, "connected": True, "message": "No data quality issues found! Great job."
            })
        
        widgets = [
            {
                "title": "Total Issues by Type",
                "chart_type": "pie",
                "chart_labels": json.dumps(labels),
                "chart_data": json.dumps(data),
                "record_count": sum(data)
            },
            {
                "title": "Affected Records List",
                "table": html_table,
                "chart_type": "none"
            },
            {
                "title": "Recommendations",
                "table": "<ul class='list-group'><li class='list-group-item'><b>Accounts:</b> Use enrichment tools to fix missing industries.</li><li class='list-group-item'><b>Contacts:</b> Run email verification or call campaigns.</li><li class='list-group-item'><b>Opportunities:</b> Close lost deals or update dates.</li></ul>",
                "chart_type": "none"
            }
        ]
        
        return templates.TemplateResponse("index.html", {
            "request": request,
            "connected": True,
            "dashboard": True,
            "dashboard_title": "Data Quality Analysis",
            "widgets": widgets
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
