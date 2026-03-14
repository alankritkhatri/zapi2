import streamlit as st
import pandas as pd
from simple_salesforce import Salesforce
import plotly.express as px
import os
from dotenv import load_dotenv
import openai

load_dotenv()

# --- Page Config ---
st.set_page_config(page_title="SF NL Query Tool", layout="wide")
st.title("📊 Salesforce Natural Language Query & Dashboard")

# --- Sidebar: Auth ---
with st.sidebar:
    st.header("Connection Settings")
    sf_user = st.text_input("Username", value=os.getenv("SALESFORCE_USERNAME") or "")
    sf_pwd = st.text_input("Password", type="password", value=os.getenv("SALESFORCE_PASSWORD") or "")
    sf_token = st.text_input("Security Token", type="password", value=os.getenv("SALESFORCE_SECURITY_TOKEN") or "")
    
    openai_key = st.text_input("OpenAI API Key", type="password", value=os.getenv("OPENAI_API_KEY") or "")
    
    if st.button("Connect to Salesforce"):
        try:
            sf = Salesforce(username=sf_user, password=sf_pwd, security_token=sf_token)
            st.session_state['sf'] = sf
            st.success("Connected!")
        except Exception as e:
            st.error(f"Failed: {str(e)}")

# --- Core Logic ---
def get_soql_from_nl(nl_query):
    """Simple NL to SOQL conversion using LLM via OpenAI."""
    if not openai_key:
        st.warning("Please provide an OpenAI API Key in the sidebar.")
        return None
    
    client = openai.OpenAI(api_key=openai_key)
    prompt = f"""
    Translate the following natural language request into a Salesforce SOQL query.
    Return ONLY the SOQL query string.
    Context: Salesforce Standard Objects (Account, Contact, Opportunity, Lead, etc.)
    Example Request: "Show me all accounts where industry is Technology"
    Example Response: SELECT Name, Industry FROM Account WHERE Industry = 'Technology'
    
    Request: {nl_query}
    SOQL:
    """
    
    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.error(f"AI Error: {str(e)}")
        return None

# --- Main Interface ---
if 'sf' in st.session_state:
    sf = st.session_state['sf']
    
    nl_input = st.text_input("Ask about your Salesforce data (e.g., 'Show me top 10 opportunities by amount')", "")

    if st.button("Query Data"):
        if nl_input:
            with st.spinner("Translating to SOQL and querying..."):
                soql = get_soql_from_nl(nl_input)
                if soql:
                    st.code(soql, language="sql")
                    try:
                        results = sf.query_all(soql)
                        records = results.get('records', [])
                        if records:
                            # Clean up records (remove metadata)
                            for r in records: r.pop('attributes', None)
                            df = pd.DataFrame(records)
                            
                            st.subheader("Data Results")
                            st.dataframe(df)

                            # --- Auto Dashboard ---
                            st.subheader("Visual Analysis")
                            col1, col2 = st.columns(2)
                            
                            num_cols = df.select_dtypes(include=['number']).columns.tolist()
                            cat_cols = df.select_dtypes(include=['object']).columns.tolist()

                            if len(num_cols) >= 1 and len(cat_cols) >= 1:
                                with col1:
                                    fig_bar = px.bar(df, x=cat_cols[0], y=num_cols[0], title=f"{num_cols[0]} by {cat_cols[0]}")
                                    st.plotly_chart(fig_bar, use_container_width=True)
                                with col2:
                                    fig_pie = px.pie(df, names=cat_cols[0], values=num_cols[0] if num_cols else None, title=f"Distribution of {cat_cols[0]}")
                                    st.plotly_chart(fig_pie, use_container_width=True)
                            else:
                                st.info("Not enough numeric/categorical data to generate charts automatically.")
                        else:
                            st.warning("No records found.")
                    except Exception as e:
                        st.error(f"Salesforce Query Error: {str(e)}")
        else:
            st.info("Enter a question in natural language above.")
else:
    st.info("👈 Please connect to Salesforce using the sidebar to get started.")

# Footer
st.markdown("---")
st.caption("Simplified Salesforce NL Search Prototype")
