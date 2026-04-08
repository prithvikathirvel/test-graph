import os
import logging
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse

# --- Assume these are your project's local imports ---
# from app.core.model import DynamicFlowReq
# from app.core.helpers import parse_log_line
# from app.services.dynamic_flow import generate_workflow_schema

# --- Mock implementations for demonstration if local imports are not available ---
# This allows the code to run standalone for UI testing.
try:
    from app.core.model import DynamicFlowReq
    from app.core.helpers import parse_log_line
    from app.services.dynamic_flow import generate_workflow_schema
except ImportError:
    import datetime
    import re
    from pydantic import BaseModel
    print("Warning: Using mock implementations for local imports.")

    class DynamicFlowReq(BaseModel):
        query: str
        session_id: str
        thread_id: str | None = None

    def parse_log_line(line: str) -> dict:
        # Improved parser for: 2026-04-08 04:09:35 | INFO    | [flow:f96952] | app.main | Message
        # Matches: Timestamp | Level | [Context] | Module | Message
        pattern = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| ([A-Z ]+) \| (\[[^\]]+\]) \| ([\w\.]+) \| (.*)$"
        match = re.match(pattern, line.strip())
        if match:
            return {
                "timestamp": match.group(1),
                "level": match.group(2).strip().upper(),
                "flow_id": match.group(3).strip(),
                "module": match.group(4),
                "message": match.group(5).strip()
            }
        
        # Fallback for simple lines or multi-line messages
        return {
            "timestamp": "",
            "level": "INFO",
            "flow_id": "system",
            "module": "raw",
            "message": line.strip()
        }

    async def generate_workflow_schema(*args, **kwargs):
        print("Mock generate_workflow_schema called.")
        pass
# --- End of Mock implementations ---


logger = logging.getLogger(__name__)
router = APIRouter()

@router.get("/health", tags=["Admin"])
async def health_check():
    return {"status": "ok"}

@router.get("/api/logs", tags=["Admin"])
async def get_logs_api(limit: int = 500):
    log_path = os.path.join(os.getcwd(), "logs", "app.log")
    try:
        if not os.path.exists(log_path):
            return JSONResponse(content={"error": "Log file not found"}, status_code=404)
            
        with open(log_path, "r") as f:
            lines = f.readlines()[-limit:]
            
            parsed = []
            for line in lines:
                entry = parse_log_line(line)
                # If it's a continuation line (no timestamp) and we have a previous entry, append it
                if not entry.get("timestamp") and parsed:
                    parsed[-1]["message"] += "\n" + entry["message"]
                else:
                    parsed.append(entry)
            
            # Return logs in chronological order for the tail-style UI
            return JSONResponse(content={"logs": parsed})
    except Exception as e:
        logger.error(f"Failed to read or parse log file: {e}", exc_info=True)
        return JSONResponse(content={"error": str(e)}, status_code=500)

@router.get("/logs", response_class=HTMLResponse, tags=["Admin"])
async def get_logs_ui():
    html_content = r"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sify Aurora | Service Monitoring</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=Fira+Code:wght@400;500;600&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-deep: #0d1117; --bg-medium: #161b22; --border: #30363d;
            --text-main: #e6edf3; --text-muted: #7d8590; --accent: #58a6ff;
            --success: #3fb950; --error: #f85149; --warn: #d29922; --info: #388bfd;
            --debug: #8b949e; --highlight-bg: #d29922; --highlight-text: #0d1117;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            background: var(--bg-deep); color: var(--text-main); font-family: 'Inter', sans-serif;
            height: 100vh; display: flex; flex-direction: column; overflow: hidden;
        }
        header {
            padding: 1rem 1.5rem; background: rgba(13, 17, 23, 0.8); backdrop-filter: blur(10px);
            -webkit-backdrop-filter: blur(10px); border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: center; z-index: 100; flex-shrink: 0;
        }
        .brand { display: flex; align-items: center; gap: 12px; }
        .brand svg { color: var(--text-muted); }
        .brand h1 { font-size: 1.15rem; font-weight: 600; }
        .status-badge {
            padding: 5px 12px; border-radius: 20px; font-size: 0.8rem; font-weight: 500;
            display: flex; align-items: center; gap: 8px; transition: all 0.3s ease;
        }
        .status-badge.connected { background: rgba(63, 185, 80, 0.1); color: var(--success); border: 1px solid rgba(63, 185, 80, 0.2); }
        .status-badge.paused { background: rgba(210, 153, 34, 0.1); color: var(--warn); border: 1px solid rgba(210, 153, 34, 0.2); }
        .status-badge.error { background: rgba(248, 81, 73, 0.1); color: var(--error); border: 1px solid rgba(248, 81, 73, 0.2); }
        .status-dot { width: 8px; height: 8px; border-radius: 50%; }
        .status-badge.connected .status-dot { background: var(--success); box-shadow: 0 0 8px var(--success); animation: pulse 2s infinite; }
        .status-badge.paused .status-dot { background: var(--warn); }
        .status-badge.error .status-dot { background: var(--error); }
        @keyframes pulse { 0% { opacity: 0.7; } 70% { opacity: 1; } 100% { opacity: 0.7; } }
        .toolbar {
            padding: 12px 1.5rem; background: var(--bg-medium); border-bottom: 1px solid var(--border);
            display: flex; gap: 16px; align-items: center; flex-shrink: 0;
        }
        .search-container { flex-grow: 1; position: relative; }
        .search-container input {
            width: 100%; background: var(--bg-deep); border: 1px solid var(--border);
            padding: 8px 12px 8px 36px; border-radius: 6px; color: var(--text-main);
            font-size: 0.875rem; outline: none; transition: border-color 0.2s;
        }
        .search-container input:focus { border-color: var(--accent); }
        .search-container .icon {
            position: absolute; left: 10px; top: 50%; transform: translateY(-50%);
            color: var(--text-muted); width: 16px; height: 16px;
        }
        .filter-group, .controls-group { display: flex; align-items: center; gap: 8px; }
        .toolbar-btn, .filter-btn {
            background: transparent; border: 1px solid var(--border); color: var(--text-muted);
            padding: 6px 12px; border-radius: 6px; font-size: 0.8rem; font-weight: 500;
            cursor: pointer; transition: all 0.2s; display: inline-flex; align-items: center; gap: 6px;
        }
        .toolbar-btn:hover, .filter-btn:hover { background-color: var(--border); color: var(--text-main); }
        .filter-btn.active { background: var(--accent); color: white; border-color: var(--accent); }
        #log-count { font-size: 0.8rem; color: var(--text-muted); white-space: nowrap; }
        main { flex-grow: 1; overflow-y: auto; background: var(--bg-deep); position: relative; }
        .log-list { list-style: none; font-family: 'Fira Code', monospace; font-size: 0.8125rem; line-height: 1.7; padding: 0.5rem 0; }
        .log-entry {
            display: grid; grid-template-columns: 170px 80px 120px 140px 1fr;
            gap: 16px; padding: 4px 1.5rem; border-bottom: 1px solid #161b22;
            transition: background-color 0.15s; align-items: baseline;
        }
        .log-entry:hover { background-color: rgba(110, 118, 129, 0.1); }
        .log-entry > span:not(.msg) { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .level { font-weight: 600; text-transform: uppercase; }
        .lvl-INFO { color: var(--info); } .lvl-ERROR { color: var(--error); }
        .lvl-WARNING { color: var(--warn); } .lvl-DEBUG { color: var(--debug); }
        .ts { color: var(--text-muted); } .flow { color: #d2a8ff; } .module { color: #ffa657; }
        .msg { color: var(--text-main); white-space: pre-wrap; word-break: break-all; }
        mark.highlight { background-color: var(--highlight-bg); color: var(--highlight-text); border-radius: 3px; padding: 1px 2px; }
        .placeholder {
            text-align: center; padding: 4rem 1rem; font-family: 'Inter', sans-serif;
            color: var(--text-muted); font-size: 1rem;
        }
        #scroll-bottom {
            position: fixed; bottom: -50px; right: 1.5rem; background: var(--accent); color: white;
            border: none; width: 44px; height: 44px; border-radius: 50%;
            display: flex; align-items: center; justify-content: center; cursor: pointer;
            box-shadow: 0 4px 12px rgba(0,0,0,0.3); opacity: 0.8; transition: all 0.3s ease;
        }
        #scroll-bottom.visible { bottom: 1.5rem; }
        #scroll-bottom:hover { opacity: 1; transform: scale(1.05); }
        main::-webkit-scrollbar { width: 10px; }
        main::-webkit-scrollbar-track { background: transparent; }
        main::-webkit-scrollbar-thumb { background: var(--border); border-radius: 5px; }
        main::-webkit-scrollbar-thumb:hover { background: #484f58; }
    </style>
</head>
<body>
    <header>
        <div class="brand">
            <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"></polyline><line x1="12" y1="19" x2="20" y2="19"></line></svg>
            <h1>Sify Aurora Logs</h1>
        </div>
        <div id="status-indicator" class="status-badge connected"><div class="status-dot"></div><span id="status-text">CONNECTED</span></div>
    </header>
    <div class="toolbar">
        <div class="search-container">
            <svg class="icon" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>
            <input type="text" id="search" placeholder="Filter logs... (e.g., error, flow:xyz, module:auth)" oninput="filterLogs()">
        </div>
        <div class="filter-group">
            <button class="filter-btn active" onclick="setFilter('ALL')">All</button>
            <button class="filter-btn" onclick="setFilter('ERROR')">Error</button>
            <button class="filter-btn" onclick="setFilter('WARNING')">Warning</button>
            <button class="filter-btn" onclick="setFilter('INFO')">Info</button>
        </div>
        <div class="controls-group">
            <span id="log-count"></span>
            <button id="pause-btn" class="toolbar-btn" onclick="togglePause()">
                <svg id="pause-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"></path></svg>
                <svg id="play-icon" width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="display:none;"><path d="M8 5v14l11-7z"></path></svg>
                <span id="pause-text">Pause</span>
            </button>
            <button class="toolbar-btn" onclick="clearLogs()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 4H8l-7 8 7 8h13a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2z"></path><line x1="18" y1="9" x2="12" y2="15"></line><line x1="12" y1="9" x2="18" y2="15"></line></svg>
                Clear
            </button>
        </div>
    </div>
    <main id="log-main"><div class="log-list" id="log-list"></div></main>
    <button id="scroll-bottom" onclick="scrollToBottom()"><svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14m-7-7l7 7 7-7"></path></svg></button>

    <script>
        const LOG_API_URL = 'api/logs?limit=500';
        const POLLING_INTERVAL = 3000;

        let allLogs = [];
        let currentLevelFilter = 'ALL';
        let isFetching = false;
        let isPaused = false;
        let poller;
        let initialLoad = true;

        const ui = {
            logContainer: document.getElementById('log-list'),
            mainContainer: document.getElementById('log-main'),
            searchInput: document.getElementById('search'),
            statusIndicator: document.getElementById('status-indicator'),
            statusText: document.getElementById('status-text'),
            pauseText: document.getElementById('pause-text'),
            pauseIcon: document.getElementById('pause-icon'),
            playIcon: document.getElementById('play-icon'),
            logCountEl: document.getElementById('log-count'),
            scrollBtn: document.getElementById('scroll-bottom'),
        };

        function setStatus(state, message) {
            ui.statusIndicator.className = 'status-badge ' + state;
            ui.statusText.textContent = message;
        }

        async function fetchLogs() {
            if (isFetching) return;
            isFetching = true;
            if (initialLoad) {
                ui.logContainer.innerHTML = `<div class="placeholder">Fetching logs...</div>`;
            }
            try {
                const response = await fetch(LOG_API_URL);
                if (!response.ok) throw new Error(`HTTP error! Status: ${response.status}`);
                const data = await response.json();
                if (data.error) throw new Error(data.error);
                
                allLogs = data.logs || [];
                renderLogs();
                if (!isPaused) setStatus('connected', 'CONNECTED');
            } catch (err) {
                console.error("Failed to fetch logs:", err);
                setStatus('error', 'ERROR');
                ui.logContainer.innerHTML = `<div class="placeholder">Error fetching logs: ${escapeHtml(err.message)}</div>`;
            } finally {
                isFetching = false;
                initialLoad = false;
            }
        }

        function highlightText(text, highlight) {
            const safeText = escapeHtml(text);
            if (!highlight) return safeText;
            const regex = new RegExp(escapeRegExp(highlight), 'gi');
            return safeText.replace(regex, `<mark class="highlight">$&</mark>`);
        }

        function renderLogs() {
            const searchTerm = ui.searchInput.value.toLowerCase();
            const isScrolledToBottom = ui.mainContainer.scrollHeight - ui.mainContainer.clientHeight <= ui.mainContainer.scrollTop + 20;

            const searchFilters = [];
            const globalSearchTerms = [];
            searchTerm.split(' ').forEach(term => {
                if (term.includes(':')) {
                    const [key, ...valueParts] = term.split(':');
                    if (key && valueParts.length > 0) searchFilters.push({ key: key.trim(), value: valueParts.join(':').trim() });
                } else if (term) {
                    globalSearchTerms.push(term);
                }
            });

            const filteredLogs = allLogs.filter(log => {
                if (!log) return false; // Safeguard against null/undefined logs
                
                // *** FIX: Case-insensitive level matching ***
                const matchesLevel = currentLevelFilter === 'ALL' || (log.level || '').toUpperCase() === currentLevelFilter;
                
                const matchesFieldFilters = searchFilters.every(filter => {
                    const logValue = (log[filter.key] || '').toLowerCase();
                    return logValue.includes(filter.value);
                });
                const logString = `${log.message || ''} ${log.flow_id || ''} ${log.module || ''}`.toLowerCase();
                const matchesGlobalSearch = globalSearchTerms.every(term => logString.includes(term));
                return matchesLevel && matchesFieldFilters && matchesGlobalSearch;
            });
            
            if (filteredLogs.length === 0) {
                 if (allLogs.length === 0 && !initialLoad) {
                    ui.logContainer.innerHTML = `<div class="placeholder">Log file appears to be empty or is unavailable.</div>`;
                } else {
                    ui.logContainer.innerHTML = `<div class="placeholder">No logs match your current filters.</div>`;
                }
            } else {
                const globalSearchTerm = globalSearchTerms.join(' ');
                const html = filteredLogs.map(log => `
                    <div class="log-entry">
                        <span class="ts">${escapeHtml(log.timestamp)}</span>
                        <span class="level lvl-${escapeHtml(log.level)}">${escapeHtml(log.level)}</span>
                        <span class="flow" title="${escapeHtml(log.flow_id)}">${highlightText(log.flow_id, globalSearchTerm)}</span>
                        <span class="module" title="${escapeHtml(log.module)}">${highlightText(log.module, globalSearchTerm)}</span>
                        <span class="msg">${highlightText(log.message, globalSearchTerm)}</span>
                    </div>
                `).join('');
                ui.logContainer.innerHTML = html;
            }

            ui.logCountEl.textContent = `${filteredLogs.length} / ${allLogs.length}`;
            if (isScrolledToBottom) scrollToBottom();
        }

        function setFilter(level) {
            currentLevelFilter = level;
            document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));
            const activeBtn = Array.from(document.querySelectorAll('.filter-btn')).find(btn => btn.textContent.toUpperCase() === level);
            if (activeBtn) activeBtn.classList.add('active');
            renderLogs();
        }

        function filterLogs() { renderLogs(); }
        function clearLogs() { allLogs = []; renderLogs(); }
        function togglePause() {
            isPaused = !isPaused;
            ui.pauseText.textContent = isPaused ? 'Resume' : 'Pause';
            ui.pauseIcon.style.display = isPaused ? 'none' : 'inline-block';
            ui.playIcon.style.display = isPaused ? 'inline-block' : 'none';
            if (isPaused) {
                stopPolling();
                setStatus('paused', 'PAUSED');
            } else {
                startPolling();
            }
        }

        function startPolling() {
            if (poller) clearTimeout(poller);
            const poll = async () => {
                await fetchLogs();
                if (!isPaused) poller = setTimeout(poll, POLLING_INTERVAL);
            };
            poll();
        }

        function stopPolling() { clearTimeout(poller); }
        function scrollToBottom() { ui.mainContainer.scrollTop = ui.mainContainer.scrollHeight; }
        function escapeHtml(text = '') {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        function escapeRegExp(string) { return string.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
        
        ui.mainContainer.addEventListener('scroll', () => {
            const isAtBottom = ui.mainContainer.scrollHeight - ui.mainContainer.clientHeight <= ui.mainContainer.scrollTop + 1;
            ui.scrollBtn.classList.toggle('visible', !isAtBottom);
        });

        startPolling();
    </script>
</body>
</html>
    """
    return HTMLResponse(content=html_content)

@router.post("/dynamic-flow", tags=["Agent Flows"])
async def dynamic_flow_api(req: DynamicFlowReq, request: Request):
    """Iterative AI Architect: Generates or updates an Agent Flow Builder JSON Schema."""
    mongo_client = request.app.state.mongo_client
    try:
        await generate_workflow_schema(
            user_query=req.query, 
            mongo_client=mongo_client, 
            session_id=req.session_id,
            thread_id=req.thread_id
        )
        return JSONResponse(content={"success": True, "message": "Flow generated successfully"})
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.error(f"Dynamic Flow Generation Error: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error during flow generation.")