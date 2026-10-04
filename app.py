"""Chiparama Sourcing - recruiter app (Databricks App, Streamlit)."""
import os, re, datetime
import pandas as pd
import streamlit as st
from databricks import sql
from databricks.sdk import WorkspaceClient
from databricks.sdk.core import Config

st.set_page_config(page_title="Chiparama Sourcing", layout="wide")
cfg = Config()
WH = os.environ["DATABRICKS_WAREHOUSE_ID"]
S = "workspace.sourcing"
STATUSES = ["New", "InMail sent", "Replied", "Call scheduled", "Screened - interested",
            "Not interested", "Submitted to client", "Rejected", "Unreachable"]
USER = (st.context.headers.get("X-Forwarded-Email") or st.context.headers.get("X-Forwarded-Preferred-Username") or "unknown")

@st.cache_resource
def conn():
    return sql.connect(server_hostname=cfg.host, http_path=f"/sql/1.0/warehouses/{WH}",
                       credentials_provider=lambda: cfg.authenticate)

def q(query, params=None) -> pd.DataFrame:
    with conn().cursor() as c:
        c.execute(query, params or {})
        return c.fetchall_arrow().to_pandas() if c.description else pd.DataFrame()

def run(query, params=None):
    with conn().cursor() as c:
        c.execute(query, params or {})

@st.cache_resource
def ws():
    return WorkspaceClient()

@st.cache_resource
def job_id():
    """Job id from SOURCING_JOB_ID if set, otherwise looked up by name."""
    if os.environ.get("SOURCING_JOB_ID"):
        return int(os.environ["SOURCING_JOB_ID"])
    name = os.environ.get("SOURCING_JOB_NAME", "Chiparama Sourcing - JD to shortlist")
    found = list(ws().jobs.list(name=name))
    if not found:
        st.error(f"Job '{name}' not visible to the app. Run notebook 07 to give the app 'Can manage run' on it.")
        st.stop()
    return found[0].job_id

st.sidebar.title("Chiparama Sourcing")
st.sidebar.caption(f"Signed in as {USER}")
page = st.sidebar.radio("Go to", ["Call list", "New JD", "Sourcing runs"])

# ---------------------------------------------------------------- New JD
if page == "New JD":
    st.header("Add a JD and start sourcing")
    with st.form("jd"):
        c1, c2 = st.columns(2)
        title = c1.text_input("Role title", placeholder="Senior Physical Design Engineer")
        location = c2.text_input("Location", value="Bangalore")
        keywords = c1.text_input("LinkedIn search keywords (job title)", placeholder="physical design engineer",
                                 help="What candidates' current/past job title should contain. Use OR for variants.")
        max_c = c2.slider("Candidates to source", 25, 200, 50, step=25,
                          help="Keep it modest - this uses Prashant's LinkedIn Recruiter account.")
        jd_text = st.text_area("Paste the full JD (must-haves, nice-to-haves, not-a-fit)", height=320)
        go = st.form_submit_button("Save JD and start sourcing", type="primary")
    if go:
        if not (title and keywords and location and len(jd_text) > 100):
            st.error("Fill in title, keywords, location and the full JD.")
        else:
            jd_id = re.sub(r"[^a-z0-9]+", "_", f"{title} {location}".lower()).strip("_")[:40] + \
                    "_" + datetime.datetime.now().strftime("%m%d%H%M")
            run(f"""INSERT INTO {S}.jds VALUES (:id, :t, :txt, :kw, :loc, current_timestamp())""",
                {"id": jd_id, "t": title, "txt": jd_text, "kw": keywords, "loc": location})
            try:
                r = ws().jobs.run_now(job_id=job_id(), job_parameters={
                    "jd_id": jd_id, "jd_title": title, "jd_path": "", "keywords": keywords,
                    "location": location, "max_candidates": str(max_c), "mode": "api", "rescreen": "false"})
                st.success(f"Sourcing started for **{jd_id}**. The call list is usually ready in 5-15 minutes "
                           f"(see *Sourcing runs*).")
            except Exception as e:
                msg = str(e)
                st.error("JD saved, but sourcing could not start" +
                         (" - another sourcing run is in progress. Try again when it finishes." if "concurrent" in msg.lower() else f": {msg[:300]}"))

# ---------------------------------------------------------------- Runs
elif page == "Sourcing runs":
    st.header("Sourcing runs")
    rows = []
    for r in ws().jobs.list_runs(job_id=job_id(), limit=20):
        p = {x.name: x.value for x in (r.job_parameters or [])}
        rows.append({"JD": p.get("jd_id"), "Started": pd.to_datetime(r.start_time, unit="ms") + pd.Timedelta(hours=5, minutes=30),
                     "State": (r.state.result_state or r.state.life_cycle_state).value if r.state else "",
                     "Candidates": p.get("max_candidates"), "Link": r.run_page_url})
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 column_config={"Link": st.column_config.LinkColumn("Details", display_text="open"),
                                "Started": st.column_config.DatetimeColumn("Started (IST)", format="DD MMM, HH:mm")})
    if st.button("Refresh"): st.rerun()

# ---------------------------------------------------------------- Call list
else:
    st.header("Call list")
    jds = q(f"""SELECT j.jd_id, j.title, j.location, j.created_at, count(r.candidate_id) AS screened
                FROM {S}.jds j LEFT JOIN {S}.screening_results r USING (jd_id)
                GROUP BY ALL ORDER BY j.created_at DESC""")
    if jds.empty:
        st.info("No JDs yet - add one under *New JD*."); st.stop()
    label = {r.jd_id: f"{r.title} · {r.location} · {r.screened} screened ({r.jd_id})" for r in jds.itertuples()}
    jd_id = st.selectbox("JD", jds.jd_id, format_func=label.get)

    df = q(f"""
      SELECT r.candidate_id, r.verdict, r.fit_score, r.name, r.headline, r.location, r.pd_years,
             array_join(r.verify_on_call, ' | ') AS ask_on_call, r.strengths, r.concerns, r.outreach_hook,
             array_join(r.must_haves_missing, ', ') AS gaps, r.public_profile_url AS linkedin,
             coalesce(s.status, 'New') AS status, s.owner, s.notes, s.updated_by, s.updated_at
      FROM {S}.screening_results r
      LEFT JOIN {S}.candidate_status s ON s.jd_id = r.jd_id AND s.candidate_id = r.candidate_id
      WHERE r.jd_id = :id
      ORDER BY CASE r.verdict WHEN 'STRONG_FIT' THEN 1 WHEN 'POSSIBLE_FIT' THEN 2 WHEN 'INSUFFICIENT_DATA' THEN 3 ELSE 4 END,
               r.fit_score DESC""", {"id": jd_id})
    if df.empty:
        st.warning("No screened candidates yet for this JD - check *Sourcing runs*."); st.stop()

    m = st.columns(5)
    for col, (lbl, v) in zip(m, [("Strong fit", "STRONG_FIT"), ("Possible fit", "POSSIBLE_FIT"),
                                  ("Need data", "INSUFFICIENT_DATA"), ("Not fit", "NOT_FIT")]):
        col.metric(lbl, int((df.verdict == v).sum()))
    m[4].metric("Contacted", int((~df.status.isin(["New"])).sum()))

    f1, f2, f3 = st.columns(3)
    verdicts = f1.multiselect("Verdict", ["STRONG_FIT", "POSSIBLE_FIT", "INSUFFICIENT_DATA", "NOT_FIT"],
                              default=["STRONG_FIT", "POSSIBLE_FIT", "INSUFFICIENT_DATA"])
    statuses = f2.multiselect("Status", STATUSES, default=[])
    mine = f3.toggle("Only mine", value=False)
    view = df[df.verdict.isin(verdicts)]
    if statuses: view = view[view.status.isin(statuses)]
    if mine: view = view[view.owner == USER]

    st.caption("Edit **Status / Owner / Notes** in the table, then click *Save changes*.")
    edited = st.data_editor(
        view[["verdict", "fit_score", "name", "headline", "location", "pd_years", "ask_on_call",
              "linkedin", "status", "owner", "notes", "candidate_id"]],
        hide_index=True, use_container_width=True, key=f"ed_{jd_id}",
        disabled=["verdict", "fit_score", "name", "headline", "location", "pd_years", "ask_on_call", "linkedin", "candidate_id"],
        column_config={
            "linkedin": st.column_config.LinkColumn("LinkedIn", display_text="open"),
            "status": st.column_config.SelectboxColumn("Status", options=STATUSES, required=True),
            "fit_score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
            "pd_years": st.column_config.NumberColumn("PD yrs", format="%.1f"),
            "ask_on_call": st.column_config.TextColumn("Ask on call", width="large"),
            "candidate_id": None})
    if st.button("Save changes", type="primary"):
        before = view.set_index("candidate_id")[["status", "owner", "notes"]].fillna("")
        after = edited.set_index("candidate_id")[["status", "owner", "notes"]].fillna("")
        changed = after[(after != before.loc[after.index]).any(axis=1)]
        for cid, r in changed.iterrows():
            run(f"""MERGE INTO {S}.candidate_status t
                    USING (SELECT :jd AS jd_id, :cid AS candidate_id) s
                    ON t.jd_id = s.jd_id AND t.candidate_id = s.candidate_id
                    WHEN MATCHED THEN UPDATE SET status=:st, owner=:ow, notes=:nt, updated_by=:u, updated_at=current_timestamp()
                    WHEN NOT MATCHED THEN INSERT VALUES (:jd, :cid, :st, :ow, :nt, :u, current_timestamp())""",
                {"jd": jd_id, "cid": cid, "st": r.status, "ow": r.owner or USER, "nt": r.notes, "u": USER})
        st.success(f"Saved {len(changed)} change(s)."); st.rerun()

    st.subheader("Candidate details")
    pick = st.selectbox("Candidate", view.name.tolist())
    c = view[view.name == pick].iloc[0]
    a, b = st.columns(2)
    a.markdown(f"**{c['name']}** · {c.verdict} · score {c.fit_score}  \n{c.headline}  \n{c.location} · {c.pd_years} PD years")
    a.markdown(f"**Strengths:** {c.strengths}  \n**Concerns:** {c.concerns}  \n**Gaps:** {c.gaps or '-'}")
    b.markdown("**Ask on the call:**  \n" + "  \n".join(f"- {x}" for x in (c.ask_on_call or "").split(" | ") if x))
    b.text_area("InMail opener (copy)", c.outreach_hook, height=90)
    b.link_button("Open LinkedIn profile", c.linkedin)
