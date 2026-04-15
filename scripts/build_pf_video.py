"""
ProductFactory showcase video — web-quality slides rendered by Chromium (Playwright),
narrated by Edge TTS (en-US-AriaNeural), assembled by moviepy.

Every slide is a full HTML page styled to match design.html.
Playwright screenshots at 1280x720 → lossless PNG → moviepy clip + TTS audio.

Run:
    python scripts/build_pf_video.py
Output: output/productfactory_story_<timestamp>.mp4
"""

import asyncio
import logging
import sys
import tempfile
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger("pf_video")

W, H = 1280, 720

# ── Shared CSS injected into every slide ──────────────────────────────────────
BASE_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600&display=swap');
:root {
  --bg:     #080c14; --bg2: #0d1526; --bg3: #111e36;
  --border: rgba(99,179,237,0.12); --border2: rgba(99,179,237,0.22);
  --blue: #3b82f6; --blue2: #60a5fa; --cyan: #06b6d4;
  --violet: #8b5cf6; --violet2: #a78bfa;
  --green: #10b981; --green2: #34d399;
  --amber: #f59e0b; --amber2: #fbbf24;
  --rose: #f43f5e; --rose2: #fb7185;
  --pink: #ec4899;
  --slate: #94a3b8;
  --text: #e2e8f0; --text2: #94a3b8; --text3: #64748b;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
body {
  width: 1280px; height: 720px; overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: 'Inter', system-ui, sans-serif;
  font-size: 14px; line-height: 1.5;
  position: relative;
}
/* grid lines */
body::after {
  content: '';
  position: absolute; inset: 0; z-index: 0; pointer-events: none;
  background-image:
    linear-gradient(rgba(59,130,246,0.03) 1px, transparent 1px),
    linear-gradient(90deg, rgba(59,130,246,0.03) 1px, transparent 1px);
  background-size: 40px 40px;
}
.slide { position: relative; z-index: 1; width: 1280px; height: 720px; padding: 48px 64px; display: flex; flex-direction: column; }
.label { font-size: 11px; font-weight: 700; letter-spacing: 2px; text-transform: uppercase; color: var(--blue2); margin-bottom: 10px; }
h1 { font-size: 68px; font-weight: 900; letter-spacing: -2.5px; line-height: 1.0;
     background: linear-gradient(135deg,#e2e8f0 0%,#93c5fd 40%,#a78bfa 70%,#e2e8f0 100%);
     -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
h2 { font-size: 38px; font-weight: 800; letter-spacing: -1px; line-height: 1.1; }
.sub { color: var(--text2); font-size: 16px; margin-top: 10px; max-width: 680px; line-height: 1.6; }
.card { background: var(--bg2); border: 1px solid var(--border); border-radius: 14px; padding: 22px 24px; }
.accent-bar { height: 3px; border-radius: 3px; margin-bottom: 32px; background: linear-gradient(90deg, var(--blue), var(--violet), var(--cyan)); }
code { font-family: 'JetBrains Mono', monospace; font-size: 12px; color: var(--blue2); background: rgba(59,130,246,0.1); padding: 2px 6px; border-radius: 4px; }
.glow-1 { position: absolute; width: 600px; height: 600px; border-radius: 50%; filter: blur(120px); pointer-events: none; background: radial-gradient(circle, rgba(59,130,246,0.12) 0%, transparent 70%); top: -150px; left: -150px; }
.glow-2 { position: absolute; width: 500px; height: 500px; border-radius: 50%; filter: blur(120px); pointer-events: none; background: radial-gradient(circle, rgba(139,92,246,0.10) 0%, transparent 70%); bottom: -100px; right: -100px; }
.watermark { position: absolute; bottom: 20px; right: 40px; font-size: 12px; color: var(--text3); font-weight: 500; z-index: 2; }
"""

def _html(body: str, extra_css: str = "") -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8">
<style>{BASE_CSS}{extra_css}</style></head>
<body>
<div class="glow-1"></div>
<div class="glow-2"></div>
{body}
<div class="watermark">ProductFactory · Built with Claude Code</div>
</body></html>"""


# ── Slide HTML definitions ────────────────────────────────────────────────────

SLIDES = [

# 0 — Hero title
(_html("""
<div class="slide" style="justify-content:center;align-items:flex-start;gap:0">
  <div style="display:flex;align-items:center;gap:10px;margin-bottom:28px">
    <div style="width:9px;height:9px;border-radius:50%;background:#34d399;box-shadow:0 0 10px #34d399"></div>
    <span style="font-size:12px;font-weight:700;color:#34d399;letter-spacing:1.5px;text-transform:uppercase">Autonomous Development System</span>
  </div>
  <h1 style="font-size:80px;margin-bottom:20px">ProductFactory</h1>
  <p class="sub" style="font-size:20px;max-width:620px;margin-bottom:48px">AI agents that design, code, review and ship software — 24 hours a day, 7 days a week, with zero human intervention.</p>
  <div style="display:flex;gap:48px">
    <div><div style="font-size:44px;font-weight:900;background:linear-gradient(135deg,#60a5fa,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">11</div><div style="font-size:11px;color:var(--text3);font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-top:2px">AI Personas</div></div>
    <div><div style="font-size:44px;font-weight:900;background:linear-gradient(135deg,#60a5fa,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">24/7</div><div style="font-size:11px;color:var(--text3);font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-top:2px">Uptime</div></div>
    <div><div style="font-size:44px;font-weight:900;background:linear-gradient(135deg,#60a5fa,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">∞</div><div style="font-size:11px;color:var(--text3);font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-top:2px">Products</div></div>
    <div><div style="font-size:44px;font-weight:900;background:linear-gradient(135deg,#60a5fa,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">0</div><div style="font-size:11px;color:var(--text3);font-weight:600;letter-spacing:0.5px;text-transform:uppercase;margin-top:2px">Human PRs</div></div>
  </div>
</div>
"""),
"Introducing ProductFactory — the 24/7 autonomous development system. AI agents that design, code, review, and ship software around the clock. Eleven specialised AI personas. Unlimited products. Zero pull requests written by a human."),

# 1 — The Problem
(_html("""
<div class="slide">
  <div class="label">The Problem</div>
  <h2 style="margin-bottom:6px">Software development is broken by default</h2>
  <p class="sub" style="margin-bottom:28px">The best engineering teams still hit these walls every week.</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
    <div class="card" style="border-left:3px solid var(--rose);display:flex;gap:16px;align-items:flex-start">
      <span style="font-size:28px;margin-top:2px">⏱</span>
      <div><div style="font-weight:700;font-size:15px;margin-bottom:4px">Features take days or weeks to ship</div><div style="color:var(--text2);font-size:13px">Bottlenecks at code review, QA, and release cycles mean backlogs grow faster than teams can clear them.</div></div>
    </div>
    <div class="card" style="border-left:3px solid var(--amber);display:flex;gap:16px;align-items:flex-start">
      <span style="font-size:28px;margin-top:2px">🔁</span>
      <div><div style="font-weight:700;font-size:15px;margin-bottom:4px">Developers context-switch constantly</div><div style="color:var(--text2);font-size:13px">Writing code, reviewing PRs, writing tests, updating docs — each context switch costs 20 minutes of focus.</div></div>
    </div>
    <div class="card" style="border-left:3px solid var(--violet);display:flex;gap:16px;align-items:flex-start">
      <span style="font-size:28px;margin-top:2px">😴</span>
      <div><div style="font-weight:700;font-size:15px;margin-bottom:4px">Development stops when the team sleeps</div><div style="color:var(--text2);font-size:13px">8 hours of coding per developer per day. The other 16 hours? Nothing ships. The backlog waits.</div></div>
    </div>
    <div class="card" style="border-left:3px solid var(--cyan);display:flex;gap:16px;align-items:flex-start">
      <span style="font-size:28px;margin-top:2px">💸</span>
      <div><div style="font-weight:700;font-size:15px;margin-bottom:4px">Engineering time is the scarcest resource</div><div style="color:var(--text2);font-size:13px">Senior developers spending hours on boilerplate, documentation, and routine security reviews.</div></div>
    </div>
  </div>
</div>
"""),
"Building software is slow and expensive. Features take days or weeks. Developers lose focus to constant context-switching between coding, reviewing, testing, and documentation. Development stops completely when the team logs off. And the most expensive resource — senior engineering time — gets consumed by routine tasks."),

# 2 — The Solution
(_html("""
<div class="slide" style="justify-content:center;align-items:center;text-align:center">
  <div class="label" style="text-align:center">The Solution</div>
  <h2 style="font-size:52px;font-weight:900;letter-spacing:-2px;margin-bottom:24px;background:linear-gradient(135deg,#e2e8f0,#93c5fd,#a78bfa);-webkit-background-clip:text;-webkit-text-fill-color:transparent">What if software could build itself?</h2>
  <p style="color:var(--text2);font-size:18px;max-width:700px;line-height:1.7;margin-bottom:36px">ProductFactory gives every product its own AI engineering team. Feed it a vision and a feature backlog. It designs, implements, tests, audits security, reviews, and merges code — automatically, continuously.</p>
  <div style="display:flex;gap:24px;justify-content:center">
    <div style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:10px;padding:14px 28px;font-size:14px;font-weight:600;color:var(--green2)">✓ No developer required</div>
    <div style="background:rgba(59,130,246,0.1);border:1px solid rgba(59,130,246,0.3);border-radius:10px;padding:14px 28px;font-size:14px;font-weight:600;color:var(--blue2)">✓ No bottlenecks</div>
    <div style="background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.3);border-radius:10px;padding:14px 28px;font-size:14px;font-weight:600;color:var(--violet2)">✓ No downtime</div>
  </div>
</div>
"""),
"ProductFactory is the answer. Give it a product vision and a feature backlog, and it designs, implements, tests, audits, reviews, and merges code — automatically, continuously, with no developer required. No bottlenecks. No downtime. No context switching."),

# 3 — Architecture: Three Subsystems
(_html("""
<div class="slide">
  <div class="label">Architecture</div>
  <h2 style="margin-bottom:6px">Three Subsystems, One Machine</h2>
  <p class="sub" style="margin-bottom:28px">Three independent processes communicate over HTTP. Only the agent container touches product source code.</p>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:20px;flex:1">
    <div class="card" style="display:flex;flex-direction:column;gap:12px;position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--blue),var(--cyan))"></div>
      <div style="width:42px;height:42px;border-radius:10px;background:rgba(59,130,246,0.15);display:flex;align-items:center;justify-content:center;font-size:22px;margin-top:4px">🖥</div>
      <div style="font-size:15px;font-weight:700">Orchestrator</div>
      <code style="font-size:11px">orchestrator/poller.py</code>
      <div style="color:var(--text2);font-size:13px;line-height:1.6">Windows poller running on the host. Acquires distributed lock, selects products round-robin, launches Docker containers, live-polls session results, manages the full agent lifecycle.</div>
    </div>
    <div class="card" style="display:flex;flex-direction:column;gap:12px;position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--violet),var(--pink))"></div>
      <div style="width:42px;height:42px;border-radius:10px;background:rgba(139,92,246,0.15);display:flex;align-items:center;justify-content:center;font-size:22px;margin-top:4px">📊</div>
      <div style="font-size:15px;font-weight:700">PM Dashboard</div>
      <code style="font-size:11px">website/main.py · FastAPI</code>
      <div style="color:var(--text2);font-size:13px;line-height:1.6">FastAPI + PostgreSQL REST API and web UI. Create and approve features, watch live session logs, configure products, trigger on-demand personas, manage system settings.</div>
    </div>
    <div class="card" style="display:flex;flex-direction:column;gap:12px;position:relative;overflow:hidden">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,var(--green),var(--cyan))"></div>
      <div style="width:42px;height:42px;border-radius:10px;background:rgba(16,185,129,0.15);display:flex;align-items:center;justify-content:center;font-size:22px;margin-top:4px">🐳</div>
      <div style="font-size:15px;font-weight:700">Agent Image</div>
      <code style="font-size:11px">deploy/docker/Dockerfile</code>
      <div style="color:var(--text2);font-size:13px;line-height:1.6">Isolated Docker container per session. Claude Code runs inside with mounted OAuth tokens and SSH deploy keys. Agent reads its persona prompt, browses repo, writes code, opens PRs.</div>
    </div>
  </div>
</div>
"""),
"ProductFactory has three independent subsystems. The Orchestrator — a Windows poller — acquires a distributed lock, selects products round-robin, and manages the full agent container lifecycle. The PM Dashboard is a FastAPI website where you approve features and watch live session logs in real time. And for each session, an isolated Docker container runs Claude Code as the agent — reading its prompt, writing code, and opening pull requests."),

# 4 — 11 Personas
(_html("""
<div class="slide">
  <div class="label">Multi-Agent System</div>
  <h2 style="margin-bottom:6px">11 AI Personas — A Complete Engineering Team</h2>
  <p class="sub" style="margin-bottom:20px">Each persona is a separate Docker session with a specialized prompt, running in strict priority order.</p>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px;flex:1">
    <div class="card" style="border-left:3px solid var(--violet2);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">✏️ Designer</div><div style="font-size:11px;color:var(--text3)">Writes design docs</div></div>
    <div class="card" style="border-left:3px solid var(--blue2);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">⚙️ Coder</div><div style="font-size:11px;color:var(--text3)">Implements features · Opens PRs</div></div>
    <div class="card" style="border-left:3px solid var(--amber2);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🔍 Reviewer</div><div style="font-size:11px;color:var(--text3)">Reviews diffs · Global priority</div></div>
    <div class="card" style="border-left:3px solid var(--cyan);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🧪 QA Tester</div><div style="font-size:11px;color:var(--text3)">Adds tests to PR branch</div></div>
    <div class="card" style="border-left:3px solid var(--rose2);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🔒 Security Auditor</div><div style="font-size:11px;color:var(--text3)">OWASP checklist on every PR</div></div>
    <div class="card" style="border-left:3px solid var(--pink);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">💡 Recommender</div><div style="font-size:11px;color:var(--text3)">Competitor search · 5 new ideas</div></div>
    <div class="card" style="border-left:3px solid var(--slate);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">📝 Documenter</div><div style="font-size:11px;color:var(--text3)">README · CHANGELOG · every 3d</div></div>
    <div class="card" style="border-left:3px solid var(--green2);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">📊 Analytics</div><div style="font-size:11px;color:var(--text3)">Velocity · backlog health · 7d</div></div>
    <div class="card" style="border-left:3px solid var(--amber);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🔧 Refactorer</div><div style="font-size:11px;color:var(--text3)">Tech debt · chore features · 7d</div></div>
    <div class="card" style="border-left:3px solid var(--violet);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🏗️ DevOps</div><div style="font-size:11px;color:var(--text3)">Docker · CI · deps · 14d</div></div>
    <div class="card" style="border-left:3px solid #84cc16;padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">📋 Planner</div><div style="font-size:11px;color:var(--text3)">Generates backlog when empty</div></div>
    <div class="card" style="border-left:3px solid var(--amber2);background:rgba(245,158,11,0.06);padding:14px 16px"><div style="font-weight:700;font-size:13px;margin-bottom:3px">🎬 Product Trainer</div><div style="font-size:11px;color:var(--text3)">On-demand · showcase MP4</div></div>
  </div>
</div>
"""),
"ProductFactory fields eleven specialised AI personas — a complete engineering team. The feature delivery pipeline handles Designer, Coder, and Reviewer. The post-coder chain runs QA Tester, Security Auditor, and Recommender after every coder session. Scheduled maintenance keeps the product healthy with Documenter, Analytics, Refactorer, and DevOps. The Planner generates the backlog when it runs dry. And the Product Trainer generates showcase videos on demand."),

# 5 — Feature Pipeline (state machine)
(_html("""
<div class="slide">
  <div class="label">State Machine</div>
  <h2 style="margin-bottom:6px">Feature Lifecycle — Pending to Pushed</h2>
  <p class="sub" style="margin-bottom:28px">Every feature travels through a defined state machine. Each transition is owned by a specific persona.</p>
  <div style="display:flex;align-items:center;gap:0;flex:1">
""" + "".join([
    f"""<div style="display:flex;flex-direction:column;align-items:center;flex:1">
      <div style="width:90px;height:90px;border-radius:50%;background:{bg};border:2px solid {border};display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;gap:2px">
        <span style="font-size:22px">{emoji}</span>
        <span style="font-size:10px;font-weight:700;color:{tc}">{label}</span>
      </div>
      <div style="font-size:11px;color:var(--text3);margin-top:8px;text-align:center;max-width:90px">{desc}</div>
    </div>
    {"<div style='font-size:20px;color:var(--border2);margin-bottom:24px'>→</div>" if i < 6 else ""}"""
    for i,(emoji,label,desc,bg,border,tc) in enumerate([
      ("💤","Pending","PM creates or AI suggests","rgba(100,116,139,0.15)","#64748b","#94a3b8"),
      ("✅","Approved","PM approves in dashboard","rgba(16,185,129,0.15)","#10b981","#34d399"),
      ("✏️","Designed","Designer writes spec","rgba(139,92,246,0.15)","#8b5cf6","#a78bfa"),
      ("⚙️","Implementing","Coder building","rgba(59,130,246,0.15)","#3b82f6","#60a5fa"),
      ("🔍","Reviewing","PR open · awaiting review","rgba(245,158,11,0.15)","#f59e0b","#fbbf24"),
      ("👁","Reviewed","Reviewer approved","rgba(6,182,212,0.15)","#06b6d4","#67e8f9"),
      ("🚀","Pushed","PR merged to main","rgba(16,185,129,0.15)","#10b981","#34d399"),
    ])
  ]) + """
  </div>
  <div style="display:flex;gap:16px;margin-top:8px;flex-wrap:wrap">
    <span style="font-size:12px;background:rgba(59,130,246,0.12);color:#60a5fa;border:1px solid rgba(59,130,246,0.3);padding:3px 12px;border-radius:20px;font-weight:600">🌟 feature</span>
    <span style="font-size:12px;background:rgba(244,63,94,0.12);color:#fb7185;border:1px solid rgba(244,63,94,0.3);padding:3px 12px;border-radius:20px;font-weight:600">🐛 bug</span>
    <span style="font-size:12px;background:rgba(100,116,139,0.12);color:#94a3b8;border:1px solid rgba(100,116,139,0.3);padding:3px 12px;border-radius:20px;font-weight:600">🔧 chore</span>
    <span style="margin-left:auto;font-size:12px;color:var(--text3)">skip_design=true skips Designer → goes straight to Implementing</span>
  </div>
</div>
"""),
"Every feature follows a strict state machine. A PM approves a Pending feature. The Designer writes a spec and it becomes Designed. The Coder implements it and opens a pull request, setting it to Reviewing. The Reviewer approves the PR, setting it to Reviewed. Finally the PR is merged and the feature becomes Pushed. Features can be features, bugs, or chores. The design step can be skipped for simple changes."),

# 6 — Post-Coder Chain
(_html("""
<div class="slide">
  <div class="label">Automation Chain</div>
  <h2 style="margin-bottom:6px">Post-Coder Pipeline</h2>
  <p class="sub" style="margin-bottom:28px">After every successful coder session, three agents run automatically in sequence. No human intervention required at any step.</p>
  <div style="display:flex;align-items:center;gap:0;flex:1;padding:0 20px">
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;flex:1">
      <div style="width:96px;height:96px;border-radius:50%;background:rgba(59,130,246,0.15);border:2px solid var(--blue);display:flex;flex-direction:column;align-items:center;justify-content:center"><span style="font-size:28px">⚙️</span><span style="font-size:11px;font-weight:700;color:var(--blue2)">Coder</span></div>
      <div style="font-size:12px;color:var(--text2);text-align:center">Opens PR<br>Sets Reviewing</div>
    </div>
    <div style="flex:0.4;height:2px;background:linear-gradient(90deg,var(--blue),var(--cyan));position:relative"><div style="position:absolute;right:-8px;top:-5px;color:var(--cyan);font-size:16px">▶</div></div>
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;flex:1">
      <div style="width:96px;height:96px;border-radius:50%;background:rgba(6,182,212,0.15);border:2px solid var(--cyan);display:flex;flex-direction:column;align-items:center;justify-content:center"><span style="font-size:28px">🧪</span><span style="font-size:11px;font-weight:700;color:var(--cyan)">QA Tester</span></div>
      <div style="font-size:12px;color:var(--text2);text-align:center">Adds automated tests<br>to PR branch</div>
    </div>
    <div style="flex:0.4;height:2px;background:linear-gradient(90deg,var(--cyan),var(--rose));position:relative"><div style="position:absolute;right:-8px;top:-5px;color:var(--rose);font-size:16px">▶</div></div>
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;flex:1">
      <div style="width:96px;height:96px;border-radius:50%;background:rgba(244,63,94,0.15);border:2px solid var(--rose);display:flex;flex-direction:column;align-items:center;justify-content:center"><span style="font-size:28px">🔒</span><span style="font-size:11px;font-weight:700;color:var(--rose2)">Security</span></div>
      <div style="font-size:12px;color:var(--text2);text-align:center">OWASP audit<br>Files bug features</div>
    </div>
    <div style="flex:0.4;height:2px;background:linear-gradient(90deg,var(--rose),var(--pink));position:relative"><div style="position:absolute;right:-8px;top:-5px;color:var(--pink);font-size:16px">▶</div></div>
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;flex:1">
      <div style="width:96px;height:96px;border-radius:50%;background:rgba(236,72,153,0.15);border:2px solid var(--pink);display:flex;flex-direction:column;align-items:center;justify-content:center"><span style="font-size:28px">💡</span><span style="font-size:11px;font-weight:700;color:var(--pink)">Recommender</span></div>
      <div style="font-size:12px;color:var(--text2);text-align:center">Competitor search<br>5 new feature ideas</div>
    </div>
    <div style="flex:0.4;height:2px;background:linear-gradient(90deg,var(--pink),var(--green));position:relative"><div style="position:absolute;right:-8px;top:-5px;color:var(--green2);font-size:16px">▶</div></div>
    <div style="display:flex;flex-direction:column;align-items:center;gap:8px;flex:1">
      <div style="width:96px;height:96px;border-radius:50%;background:rgba(16,185,129,0.15);border:2px solid var(--green);display:flex;flex-direction:column;align-items:center;justify-content:center"><span style="font-size:28px">🔍</span><span style="font-size:11px;font-weight:700;color:var(--green2)">Reviewer</span></div>
      <div style="font-size:12px;color:var(--text2);text-align:center">Next cycle<br>Reviews PR</div>
    </div>
  </div>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:16px">
    <div class="card" style="padding:12px 16px;font-size:12px;color:var(--text2)"><strong style="color:var(--text)">Tests land before review.</strong> QA pushes tests to the PR branch so the Reviewer sees complete coverage.</div>
    <div class="card" style="padding:12px 16px;font-size:12px;color:var(--text2)"><strong style="color:var(--text)">Security → backlog.</strong> Issues become priority-90 bug features, fixed in the next coder cycle.</div>
    <div class="card" style="padding:12px 16px;font-size:12px;color:var(--text2)"><strong style="color:var(--text)">Chain failures don't block.</strong> Each step is best-effort — the rest of the chain always continues.</div>
  </div>
</div>
"""),
"After every successful coder session, three agents run automatically. The QA Tester checks out the PR branch and commits automated tests. The Security Auditor runs the full OWASP checklist — SQL injection, cross-site scripting, hardcoded secrets, missing authentication — and files bug features for anything it finds. The Recommender searches competitors and posts five new feature ideas. Then the Reviewer takes priority in the next poller cycle, seeing the complete test suite the QA Tester added."),

# 7 — Review & Auto-Merge
(_html("""
<div class="slide">
  <div class="label">Quality Gates</div>
  <h2 style="margin-bottom:6px">Review, Auto-Merge &amp; Confidence</h2>
  <p class="sub" style="margin-bottom:24px">The Reviewer is the quality gate. Auto-merge removes the human bottleneck while preserving correctness.</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;flex:1">
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="card" style="border-left:3px solid var(--green);flex:1">
        <div style="font-weight:700;color:var(--green2);margin-bottom:10px;font-size:14px">✓ Approve — High Confidence</div>
        <ul style="color:var(--text2);font-size:13px;padding-left:16px;display:flex;flex-direction:column;gap:5px">
          <li>All acceptance criteria met</li>
          <li>Tests pass, no coverage regression</li>
          <li>No security issues found</li>
          <li>Focused diff, easy to reason about</li>
          <li>Follows ARCHITECTURE.md conventions</li>
        </ul>
        <div style="margin-top:12px;font-size:12px;background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.3);border-radius:6px;padding:8px 12px;color:var(--green2)">🚀 Auto-merged immediately when <code>auto_merge_enabled=true</code></div>
      </div>
      <div class="card" style="border-left:3px solid var(--amber)">
        <div style="font-weight:700;color:var(--amber2);margin-bottom:8px;font-size:14px">~ Approve — Low Confidence</div>
        <div style="color:var(--text2);font-size:13px">Missing design doc, minimal tests, complex diff, or touches critical paths (auth, DB schema, payments).</div>
        <div style="margin-top:10px;font-size:12px;color:var(--text3)">Waits for human review before merge.</div>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="card" style="border-left:3px solid var(--rose);flex:1">
        <div style="font-weight:700;color:var(--rose2);margin-bottom:10px;font-size:14px">✗ Request Changes</div>
        <ul style="color:var(--text2);font-size:13px;padding-left:16px;display:flex;flex-direction:column;gap:5px">
          <li>Tests fail</li>
          <li>Acceptance criteria not met</li>
          <li>Security issues found</li>
          <li>Significant deviation from design doc</li>
        </ul>
        <div style="margin-top:12px;font-size:12px;background:rgba(244,63,94,0.1);border:1px solid rgba(244,63,94,0.3);border-radius:6px;padding:8px 12px;color:var(--rose2)">↩ Feature set back to Implementing for rework</div>
      </div>
      <div class="card" style="border-left:3px solid var(--blue)">
        <div style="font-weight:700;color:var(--blue2);margin-bottom:8px;font-size:14px">🔐 Security Checklist (every PR)</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:4px">
          <span style="font-size:11px;background:rgba(59,130,246,0.1);color:var(--blue2);border:1px solid rgba(59,130,246,0.2);padding:2px 8px;border-radius:12px">SQL Injection</span>
          <span style="font-size:11px;background:rgba(59,130,246,0.1);color:var(--blue2);border:1px solid rgba(59,130,246,0.2);padding:2px 8px;border-radius:12px">XSS</span>
          <span style="font-size:11px;background:rgba(59,130,246,0.1);color:var(--blue2);border:1px solid rgba(59,130,246,0.2);padding:2px 8px;border-radius:12px">Hardcoded Secrets</span>
          <span style="font-size:11px;background:rgba(59,130,246,0.1);color:var(--blue2);border:1px solid rgba(59,130,246,0.2);padding:2px 8px;border-radius:12px">Missing Auth</span>
          <span style="font-size:11px;background:rgba(59,130,246,0.1);color:var(--blue2);border:1px solid rgba(59,130,246,0.2);padding:2px 8px;border-radius:12px">Unvalidated Input</span>
        </div>
      </div>
    </div>
  </div>
</div>
"""),
"The Reviewer applies a strict quality gate to every pull request. A high-confidence approval means all acceptance criteria are met, tests pass, no security issues, and the diff is focused. When auto-merge is enabled, those PRs merge immediately — no human required. Low-confidence approvals wait for a human second opinion. Changes-requested sends the feature back to Implementing for rework. And the security checklist — SQL injection, cross-site scripting, hardcoded secrets, missing auth, unvalidated input — runs on every single PR."),

# 8 — Maintenance Rotation
(_html("""
<div class="slide">
  <div class="label">Scheduled Automation</div>
  <h2 style="margin-bottom:6px">Maintenance Rotation</h2>
  <p class="sub" style="margin-bottom:24px">When no feature work exists, the poller runs the next overdue maintenance persona. Scheduling state lives in <code>product.config</code> as ISO timestamps.</p>
  <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;flex:1">
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:10px">
      <div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#64748b,#94a3b8)"></div>
      <span style="font-size:32px">📝</span>
      <div style="font-size:28px;font-weight:900;color:#94a3b8">3<span style="font-size:14px;font-weight:500;margin-left:2px">days</span></div>
      <div style="font-weight:700;font-size:14px">Documenter</div>
      <div style="color:var(--text2);font-size:12px;line-height:1.5">Updates README, CHANGELOG, and ARCHITECTURE.md to match what's actually been built.</div>
      <code style="font-size:10px;margin-top:auto">last_documenter_at</code>
    </div>
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:10px">
      <div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#14b8a6,#06b6d4)"></div>
      <span style="font-size:32px">📊</span>
      <div style="font-size:28px;font-weight:900;color:#34d399">7<span style="font-size:14px;font-weight:500;margin-left:2px">days</span></div>
      <div style="font-weight:700;font-size:14px">Analytics</div>
      <div style="color:var(--text2);font-size:12px;line-height:1.5">Velocity reports, backlog health, codebase growth trends. Files data-backed feature suggestions.</div>
      <code style="font-size:10px;margin-top:auto">last_analytics_at</code>
    </div>
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:10px">
      <div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#f97316,#fbbf24)"></div>
      <span style="font-size:32px">🔧</span>
      <div style="font-size:28px;font-weight:900;color:#fbbf24">7<span style="font-size:14px;font-weight:500;margin-left:2px">days</span></div>
      <div style="font-weight:700;font-size:14px">Refactorer</div>
      <div style="color:var(--text2);font-size:12px;line-height:1.5">Identifies tech debt: god classes, N+1 queries, dead code. Creates chore features for PM approval.</div>
      <code style="font-size:10px;margin-top:auto">last_refactorer_at</code>
    </div>
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:10px">
      <div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#8b5cf6,#ec4899)"></div>
      <span style="font-size:32px">🏗️</span>
      <div style="font-size:28px;font-weight:900;color:#a78bfa">14<span style="font-size:14px;font-weight:500;margin-left:2px">days</span></div>
      <div style="font-weight:700;font-size:14px">DevOps</div>
      <div style="color:var(--text2);font-size:12px;line-height:1.5">Reviews Dockerfile, CI/CD, dependencies. Checks pinned images, non-root users, secret hygiene.</div>
      <code style="font-size:10px;margin-top:auto">last_devops_at</code>
    </div>
  </div>
</div>
"""),
"ProductFactory never lets the codebase go stale. When there's no feature work to do, the poller automatically runs the next overdue maintenance persona. Every 3 days, the Documenter updates README, CHANGELOG, and architecture docs. Every 7 days, Analytics produces velocity reports and files data-backed feature ideas. Every 7 days, the Refactorer scans for tech debt and creates chore features. And every 14 days, DevOps audits the Dockerfile, CI pipelines, and dependencies."),

# 9 — On-Demand Personas
(_html("""
<div class="slide">
  <div class="label">On-Demand Triggers</div>
  <h2 style="margin-bottom:6px">Special Personas — Bypass Round-Robin</h2>
  <p class="sub" style="margin-bottom:28px">Two personas run immediately when triggered, bypassing the normal scheduling loop entirely.</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;flex:1">
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:14px">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#f59e0b,#ec4899)"></div>
      <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
        <span style="font-size:36px">🎬</span>
        <div><div style="font-size:17px;font-weight:800">Product Trainer</div><div style="font-size:12px;color:var(--text3);margin-top:2px">Trigger: set <code>run_trainer_now = true</code> from PM dashboard</div></div>
      </div>
      <div style="color:var(--text2);font-size:13px;line-height:1.6">Generates a narrated MP4 showcase video of all shipped features using Edge TTS neural voice, Pillow slide rendering, and moviepy. Each shipped feature gets its own animated slide with narration.</div>
      <div style="background:var(--bg3);border-radius:8px;padding:12px 14px;font-size:12px;color:var(--text2)">
        <div style="font-weight:600;color:var(--text);margin-bottom:6px">Output:</div>
        <div><code>output/product_video_&lt;timestamp&gt;.mp4</code></div>
        <div style="margin-top:4px;color:var(--text3)">Committed and pushed to the product repo automatically</div>
      </div>
      <div style="font-size:12px;color:var(--text3)">Flag cleared after launch — runs exactly once per trigger. Can also run <code>scripts/build_pf_video.py</code> directly for a web-quality story video.</div>
    </div>
    <div class="card" style="position:relative;overflow:hidden;display:flex;flex-direction:column;gap:14px">
      <div style="position:absolute;top:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#06b6d4,#3b82f6)"></div>
      <div style="display:flex;align-items:center;gap:12px;margin-top:4px">
        <span style="font-size:36px">🔬</span>
        <div><div style="font-size:17px;font-weight:800">Analysis Run</div><div style="font-size:12px;color:var(--text3);margin-top:2px">Trigger: <code>POST /api/products/{id}/trigger_analysis</code></div></div>
      </div>
      <div style="color:var(--text2);font-size:13px;line-height:1.6">Deep brownfield codebase analysis. Maps the architecture, identifies technical debt hotspots, catalogues existing APIs and data models. Produces a structured report for the PM before feature planning begins.</div>
      <div style="background:var(--bg3);border-radius:8px;padding:12px 14px;font-size:12px;color:var(--text2)">
        <div style="font-weight:600;color:var(--text);margin-bottom:6px">Output:</div>
        <div><code>docs/analysis_&lt;timestamp&gt;.md</code></div>
        <div style="margin-top:4px;color:var(--text3)">Sets <code>analysis_status: completed</code> on the product</div>
      </div>
    </div>
  </div>
</div>
"""),
"Two personas operate on demand, outside the normal scheduling loop. The Product Trainer — triggered by a flag in the PM dashboard — generates a narrated showcase video of all shipped features using neural text-to-speech, Pillow slides, and moviepy. The video is committed and pushed to the product repo automatically. The Analysis Run persona performs deep brownfield codebase analysis, mapping architecture and identifying technical debt before feature planning begins."),

# 10 — Architecture & Security
(_html("""
<div class="slide">
  <div class="label">Security &amp; Isolation</div>
  <h2 style="margin-bottom:6px">Zero-Trust Agent Architecture</h2>
  <p class="sub" style="margin-bottom:24px">Agents are isolated by design. No host network access, no root, minimal mounts.</p>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;flex:1">
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="card" style="border-left:3px solid var(--blue)">
        <div style="font-weight:700;margin-bottom:8px;font-size:13px;color:var(--blue2)">🐳 Container Isolation</div>
        <ul style="color:var(--text2);font-size:12px;padding-left:14px;display:flex;flex-direction:column;gap:4px">
          <li>Non-root user <code>agent</code> (UID 1001)</li>
          <li>No <code>--privileged</code> flag</li>
          <li>Isolated bridge network — not host network</li>
          <li>PM API reachable via <code>--add-host pm-api:host-gateway</code></li>
          <li>Container name pattern: <code>pf-{product_id}-{uid}</code></li>
        </ul>
      </div>
      <div class="card" style="border-left:3px solid var(--violet)">
        <div style="font-weight:700;margin-bottom:8px;font-size:13px;color:var(--violet2)">🔑 Credential Mounts (read-only)</div>
        <ul style="color:var(--text2);font-size:12px;padding-left:14px;display:flex;flex-direction:column;gap:4px">
          <li><code>~/.claude</code> OAuth tokens — mounted <code>:ro</code></li>
          <li>Per-product SSH deploy key <code>id_ed25519_{name}</code></li>
          <li>Fallback: <code>id_ed25519_productfactory</code></li>
          <li>GitHub PAT stored in DB — fetched fresh each call</li>
        </ul>
      </div>
    </div>
    <div style="display:flex;flex-direction:column;gap:12px">
      <div class="card" style="border-left:3px solid var(--green)">
        <div style="font-weight:700;margin-bottom:8px;font-size:13px;color:var(--green2)">🌐 Network Model</div>
        <ul style="color:var(--text2);font-size:12px;padding-left:14px;display:flex;flex-direction:column;gap:4px">
          <li><code>pf-internal</code> — compose-internal only (DB ↔ website)</li>
          <li><code>productfactory-net</code> — external bridge (PM API ↔ agents)</li>
          <li>Agents cannot reach each other or the host filesystem</li>
        </ul>
      </div>
      <div class="card" style="border-left:3px solid var(--amber)">
        <div style="font-weight:700;margin-bottom:8px;font-size:13px;color:var(--amber2)">🔒 Website Auth</div>
        <ul style="color:var(--text2);font-size:12px;padding-left:14px;display:flex;flex-direction:column;gap:4px">
          <li>HTTP Basic Auth with <code>secrets.compare_digest</code> (timing-safe)</li>
          <li>Internal REST API <code>/api/...</code> — no auth (poller-only)</li>
          <li>PM status transitions gated by whitelist in <code>schemas.py</code></li>
          <li>Agents bypass the whitelist via internal API — unrestricted</li>
        </ul>
      </div>
      <div class="card" style="border-left:3px solid var(--rose)">
        <div style="font-weight:700;margin-bottom:8px;font-size:13px;color:var(--rose2)">⏱ Session Management</div>
        <div style="color:var(--text2);font-size:12px">90-min timeout hard kills container. Heartbeat via <code>progress.md</code> push — stale after 45 min triggers kill + relaunch. Distributed poller lock with 30s TTL heartbeat prevents split-brain.</div>
      </div>
    </div>
  </div>
</div>
"""),
"Security and isolation are built in from the start. Agent containers run as a non-root user with no privileged access on an isolated bridge network. OAuth tokens and SSH deploy keys are mounted read-only. The GitHub PAT is stored in the database and fetched fresh on each API call — never in environment variables. The PM dashboard uses timing-safe HTTP Basic Auth. Sessions are hard-killed after 90 minutes, and a heartbeat monitor relaunches stale containers automatically."),

# 11 — Built on Claude
(_html("""
<div class="slide" style="justify-content:center">
  <div class="label">The AI Backbone</div>
  <h2 style="font-size:46px;margin-bottom:16px">Built on Claude Code</h2>
  <p class="sub" style="font-size:16px;margin-bottom:32px;max-width:720px">Every agent — Designer, Coder, Reviewer, QA, Security and more — runs as a Claude Code session inside an isolated Docker container. Claude reads its persona prompt, explores the repository, writes production code, and reports back through a live-polled session file.</p>
  <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;max-width:960px">
    <div class="card" style="text-align:center;padding:20px">
      <div style="font-size:28px;margin-bottom:8px">🧠</div>
      <div style="font-weight:700;margin-bottom:6px;font-size:13px">Real Tool Use</div>
      <div style="color:var(--text2);font-size:12px">Browses files, runs tests, calls GitHub CLI, reads design docs — real agentic actions, not simulated steps</div>
    </div>
    <div class="card" style="text-align:center;padding:20px">
      <div style="font-size:28px;margin-bottom:8px">📄</div>
      <div style="font-weight:700;margin-bottom:6px;font-size:13px">session_result.json</div>
      <div style="color:var(--text2);font-size:12px">Agents write NDJSON lines as they complete each feature. Live-polled every 30s — DB updates in real time, mid-session</div>
    </div>
    <div class="card" style="text-align:center;padding:20px">
      <div style="font-size:28px;margin-bottom:8px">🔄</div>
      <div style="font-weight:700;margin-bottom:6px;font-size:13px">Ollama Fallback</div>
      <div style="color:var(--text2);font-size:12px">Set <code>USE_OLLAMA=1</code> to run fully locally — same tool-use loop, no Claude API key required</div>
    </div>
  </div>
  <div style="margin-top:24px;background:rgba(59,130,246,0.06);border:1px solid rgba(59,130,246,0.2);border-radius:10px;padding:16px 24px;max-width:960px;display:flex;gap:16px;align-items:center">
    <span style="font-size:24px">⚡</span>
    <div style="font-size:13px;color:var(--text2)">Powered by <strong style="color:var(--text)">Anthropic's Claude Sonnet 4.6</strong> — the same model used to build ProductFactory itself. Model selection, session timeout, and feature caps are all runtime-configurable from the PM dashboard.</div>
  </div>
</div>
"""),
"Under the hood, every agent is Claude Code — Anthropic's most capable model — running inside an isolated Docker container. Claude reads its persona prompt, browses the repository with real tool use, writes production code, calls the GitHub CLI to open pull requests, and writes results back through a live-polled session file that updates the database every 30 seconds. Need to run locally without an API key? Set USE_OLLAMA=1 to use the same tool-use loop against a local Ollama model."),

# 12 — Outro
(_html("""
<div class="slide" style="justify-content:center;align-items:center;text-align:center">
  <div style="display:flex;align-items:center;justify-content:center;gap:10px;margin-bottom:32px">
    <div style="width:10px;height:10px;border-radius:50%;background:#34d399;box-shadow:0 0 14px #34d399"></div>
    <span style="font-size:12px;font-weight:700;color:#34d399;letter-spacing:1.5px;text-transform:uppercase">Always Running</span>
  </div>
  <h1 style="font-size:84px;margin-bottom:16px">ProductFactory</h1>
  <p style="font-size:28px;font-weight:700;letter-spacing:-0.5px;margin-bottom:24px;background:linear-gradient(135deg,#60a5fa,#a78bfa,#06b6d4);-webkit-background-clip:text;-webkit-text-fill-color:transparent">Code While You Sleep</p>
  <p style="color:var(--text2);font-size:16px;max-width:640px;line-height:1.7;margin-bottom:40px">An autonomous engineering team that never clocks out. From idea to merged pull request — with zero human intervention required.</p>
  <div style="display:flex;gap:16px;justify-content:center;flex-wrap:wrap">
    <div style="background:rgba(59,130,246,0.1);border:1px solid rgba(59,130,246,0.25);border-radius:8px;padding:10px 20px;font-size:13px;color:var(--blue2);font-weight:600">11 AI Personas</div>
    <div style="background:rgba(16,185,129,0.1);border:1px solid rgba(16,185,129,0.25);border-radius:8px;padding:10px 20px;font-size:13px;color:var(--green2);font-weight:600">24/7 Development</div>
    <div style="background:rgba(139,92,246,0.1);border:1px solid rgba(139,92,246,0.25);border-radius:8px;padding:10px 20px;font-size:13px;color:var(--violet2);font-weight:600">Zero Human PRs</div>
    <div style="background:rgba(6,182,212,0.1);border:1px solid rgba(6,182,212,0.25);border-radius:8px;padding:10px 20px;font-size:13px;color:var(--cyan);font-weight:600">Powered by Claude</div>
  </div>
</div>
"""),
"ProductFactory. An autonomous engineering team that never clocks out. Eleven specialised AI personas. 24 hours a day, 7 days a week. Zero pull requests written by a human. From idea to merged code — with zero human intervention required. Code while you sleep."),

]

NARRATIONS = [s[1] for s in SLIDES]
SLIDE_HTML = [s[0] for s in SLIDES]


def _screenshot_slides(tmp_path: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright
    paths = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": W, "height": H})
        for i, html in enumerate(SLIDE_HTML):
            log.info(f"  Screenshot slide {i+1}/{len(SLIDE_HTML)}")
            page.set_content(html, wait_until="networkidle")
            # Give web fonts a moment to render
            page.wait_for_timeout(600)
            out = tmp_path / f"slide_{i:02d}.png"
            page.screenshot(path=str(out), clip={"x": 0, "y": 0, "width": W, "height": H})
            paths.append(out)
        browser.close()
    return paths


def _synth_audio(narrations: list[str], tmp_path: Path) -> list[Path]:
    import edge_tts

    async def _run_all():
        paths = []
        for i, text in enumerate(narrations):
            log.info(f"  TTS slide {i+1}/{len(narrations)}")
            out = tmp_path / f"audio_{i:02d}.mp3"
            comm = edge_tts.Communicate(text, voice="en-US-AriaNeural")
            await comm.save(str(out))
            paths.append(out)
        return paths

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run_all())
    finally:
        loop.close()


def build(output_dir: Path) -> Path:
    try:
        import edge_tts          # noqa
        from moviepy import ImageClip, AudioFileClip, concatenate_videoclips  # noqa
        from playwright.sync_api import sync_playwright  # noqa
    except ImportError as e:
        log.error(f"Missing dependency: {e}")
        sys.exit(1)

    from moviepy import ImageClip, AudioFileClip, concatenate_videoclips

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = output_dir / f"productfactory_story_{timestamp}.mp4"
    log.info(f"Building web-quality ProductFactory video → {video_path}")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        log.info("Phase 1: Rendering slides with Chromium…")
        slide_paths = _screenshot_slides(tmp_path)

        log.info("Phase 2: Synthesising narration with Edge TTS…")
        audio_paths = _synth_audio(NARRATIONS, tmp_path)

        log.info("Phase 3: Assembling video…")
        clips = []
        for i, (slide, audio) in enumerate(zip(slide_paths, audio_paths)):
            pause = 1.5 if i == len(slide_paths) - 1 else 0.4
            a = AudioFileClip(str(audio))
            c = ImageClip(str(slide)).with_duration(a.duration + pause).with_audio(a)
            clips.append(c)

        final = None
        try:
            final = concatenate_videoclips(clips, method="compose")
            final.write_videofile(
                str(video_path), fps=24,
                codec="libx264", audio_codec="aac",
                logger=None,
            )
            log.info(f"Done! → {video_path}  ({video_path.stat().st_size // 1024 // 1024} MB)")
            return video_path
        finally:
            if final:
                try: final.close()
                except Exception: pass
            for c in clips:
                try: c.close()
                except Exception: pass


if __name__ == "__main__":
    repo_root = Path(__file__).parent.parent
    build(repo_root / "output")
