from fastapi import FastAPI, Request, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
from simple_salesforce import Salesforce
import os
from dotenv import load_dotenv
import openai
import pandas as pd

load_dotenv()

app = FastAPI()
templates = Jinja2Templates(directory="templates")

# Cache for SF connection (In-memory for simplicity)
sf_connection = None

import subprocess
import json

def get_sf_auth_from_cli():
    """Retrieve Access Token and Instance URL from Salesforce CLI."""
    try:
        # Get data from default org or current org session
        result = subprocess.run(['sf', 'org', 'display', '--json'], capture_output=True, text=True, shell=True)
        if result.returncode != 0:
            return None, None
        
        data = json.loads(result.stdout)
        access_token = data['result']['accessToken']
        instance_url = data['result']['instanceUrl']
        return access_token, instance_url
    except Exception as e:
        print(f"CLI Error: {e}")
        return None, None

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    global sf_connection
    token, url = get_sf_auth_from_cli()
    if token and url and not sf_connection:
        try:
            sf_connection = Salesforce(instance_url=url, session_id=token)
            return templates.TemplateResponse("index.html", {"request": request, "connected": True, "message": "Authenticated via SF CLI!"})
        except:
            pass
    return templates.TemplateResponse("index.html", {"request": request, "connected": sf_connection is not None})

@app.post("/connect")
async def connect(request: Request, username: str = Form(...), password: str = Form(...), token: str = Form(...), openai_key: str = Form(...)):
    global sf_connection
    try:
        # Switching to domain-based login which often works when standard SOAP login is restricted
        sf_connection = Salesforce(
            username=username, 
            password=password, 
            security_token=token,
            domain='test' if '.develop.' in username or '.sandbox.' in username else None
        )
        os.environ["OPENAI_API_KEY"] = openai_key
        return templates.TemplateResponse("index.html", {"request": request, "connected": True, "message": "Connected successfully!"})
    except Exception as e:
        # Fallback: Try with specific instance URL if domain detection fails
        try:
             sf_connection = Salesforce(
                username=username, 
                password=password, 
                security_token=token,
                instance_url="https://orgfarm-2e4b2bbcce-dev-ed.develop.my.salesforce.com"
            )
             os.environ["OPENAI_API_KEY"] = openai_key
             return templates.TemplateResponse("index.html", {"request": request, "connected": True, "message": "Connected via Instance URL!"})
        except:
            return templates.TemplateResponse("index.html", {"request": request, "connected": False, "error": f"Salesforce restriction: {str(e)}"})

@app.post("/query")
async def query(request: Request, nl_query: str = Form(...)):
    if not sf_connection:
        return templates.TemplateResponse("index.html", {"request": request, "connected": False, "error": "Not connected to Salesforce"})
    
    openai_key = os.getenv("OPENAI_API_KEY")
    client = openai.OpenAI(api_key=openai_key)
    
    
    # Intent Classification & Extraction
    system_prompt = """
    You are a Salesforce assistant. Analyze the user request.
    If it is a READ/QUERY request, translate it to a SOQL query (SELECT statements only).
    If it is a WRITE/CREATE request, extract the Salesforce Object API Name (e.g., Account, Contact, Lead) and the field values as a dictionary.

    Response Format (JSON only):
    For QUERY: {"action": "query", "soql": "SELECT Id, Name FROM Account LIMIT 10"}
    For CREATE: {"action": "create", "object": "Account", "data": {"Name": "New Co", "Industry": "Tech"}}

    Return ONLY raw JSON, no markdown.
    """

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": nl_query}
            ],
            temperature=0
        )
        
        content = response.choices[0].message.content.strip()
        # Clean up potential markdown code blocks
        if content.startswith("```json"):
            content = content[7:-3]
        elif content.startswith("```"):
            content = content[3:-3]
            
        intent = json.loads(content)
        
        if intent["action"] == "query":
            soql = intent["soql"]
            # Execute Query
            results = sf_connection.query_all(soql)
            records = results.get('records', [])
            
            # Prepare for Display
            if records:
                for r in records: 
                    if 'attributes' in r:
                        del r['attributes']
                
                df = pd.DataFrame(records)
                # Minimal styling for the table, removed bootstrap striped class
                html_table = df.to_html(classes='table table-hover table-sm', index=False, border=0)
                
                # Simple Chart Config (using Chart.js in frontend)
                labels = []
                data = []
                num_cols = df.select_dtypes(include=['number']).columns.tolist()
                cat_cols = df.select_dtypes(include=['object']).columns.tolist()
                
                if num_cols and cat_cols:
                    labels = df[cat_cols[0]].head(10).tolist()
                    data = df[num_cols[0]].head(10).tolist()
                    chart_title = f"{num_cols[0]} by {cat_cols[0]}"
                else:
                    chart_title = "Data Table"

                return templates.TemplateResponse("index.html", {
                    "request": request, 
                    "connected": True, 
                    "soql": soql, 
                    "table": html_table,
                    "chart_labels": labels,
                    "chart_data": data,
                    "chart_title": chart_title
                })
            else:
                return templates.TemplateResponse("index.html", {"request": request, "connected": True, "message": f"Query executed successfully but returned no records. ({soql})"})

        elif intent["action"] == "create":
            obj_name = intent["object"]
            data = intent["data"]
            
            # Dynamically access the object manager
            sf_obj = getattr(sf_connection, obj_name)
            result = sf_obj.create(data)
            
            if result.get('success'):
                msg = f"Successfully created new {obj_name}: {data}. ID: {result.get('id')}"
            else:
                msg = f"Failed to create {obj_name}: {result.get('errors')}"
                
            return templates.TemplateResponse("index.html", {"request": request, "connected": True, "message": msg})

        else:
             return templates.TemplateResponse("index.html", {"request": request, "connected": True, "error": "Could not understand the intent."})
            
    except json.JSONDecodeError:
        return templates.TemplateResponse("index.html", {"request": request, "connected": True, "error": "Failed to parse AI response. Try again."})
    except Exception as e:
        return templates.TemplateResponse("index.html", {"request": request, "connected": True, "error": str(e)})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)
