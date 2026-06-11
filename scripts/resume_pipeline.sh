#!/bin/bash
# Resume India PDF fetch + NLP pipeline from where it stopped.
# Safe to run anytime — already-fetched docs are automatically skipped.

cd "$(dirname "$0")/.."
VENV=".venv/bin/python3"

echo "=== Resuming India pipeline ==="

# Kill any stale processes first
pkill -f "india_pdf_worker|india_nlp_worker|nlp_after_pdf" 2>/dev/null && echo "Stopped old processes" || true
sleep 2

# PDF Workers — covering remaining quarters
# W1 (Apr2020-Mar2021): DONE
# W3 (Apr2022-Mar2023): DONE
# Only restart the ones with remaining work:

nohup $VENV /tmp/india_pdf_worker.py --from-date 2021-04-01 --to-date 2022-03-31 --workers 3 --log /tmp/pdf_w2.log > /tmp/pdf_w2.log 2>&1 &
echo "PDF-W2 started (Apr2021-Mar2022): PID $!"

nohup $VENV /tmp/india_pdf_worker.py --from-date 2023-04-01 --to-date 2024-03-31 --workers 3 --log /tmp/pdf_w4.log > /tmp/pdf_w4.log 2>&1 &
echo "PDF-W4 started (Apr2023-Mar2024): PID $!"

nohup $VENV /tmp/india_pdf_worker.py --from-date 2024-04-01 --to-date 2025-03-31 --workers 3 --log /tmp/pdf_w5.log > /tmp/pdf_w5.log 2>&1 &
echo "PDF-W5 started (Apr2024-Mar2025): PID $!"

nohup $VENV /tmp/india_pdf_worker.py --from-date 2025-04-01 --to-date 2026-05-30 --workers 3 --log /tmp/pdf_w6.log > /tmp/pdf_w6.log 2>&1 &
echo "PDF-W6 started (Apr2025-May2026): PID $!"

# Update NLP monitor with current PIDs
sleep 3
PDF_PIDS=$(ps aux | grep india_pdf_worker | grep -v grep | awk '{print $2}' | tr '\n' ',')
echo "PDF workers running: $PDF_PIDS"

# NLP auto-starts when all PDF workers finish
cat > /tmp/nlp_after_pdf.py << 'EOF'
import subprocess, time, logging, sys, os

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.FileHandler("/tmp/nlp_after_pdf.log", mode='a'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

import re
log = open('/tmp/nlp_after_pdf.log').read() if os.path.exists('/tmp/nlp_after_pdf.log') else ''
VENV = "/Users/makkenasrinivas/PycharmProjects/MakroGraphIntelligence/.venv/bin/python3"

def is_running(pid):
    try: os.kill(pid, 0); return True
    except: return False

# Get current PDF worker PIDs dynamically
import subprocess
result = subprocess.run(['pgrep', '-f', 'india_pdf_worker'], capture_output=True, text=True)
PDF_PIDS = [int(p) for p in result.stdout.strip().split('\n') if p.strip()]
logger.info(f"Watching PDF workers: {PDF_PIDS}")

while True:
    alive = [p for p in PDF_PIDS if is_running(p)]
    if not alive:
        logger.info("All PDF workers done! Starting NLP workers...")
        break
    logger.info(f"PDF workers running: {alive} — checking in 5 min")
    time.sleep(300)

NLP_WORKERS = [
    ("2020-01-01", "2021-12-31", "/tmp/nlp_a.log"),
    ("2022-01-01", "2023-12-31", "/tmp/nlp_b.log"),
    ("2024-01-01", "2026-06-30", "/tmp/nlp_c.log"),
]
procs = []
for frm, to, log in NLP_WORKERS:
    cmd = [VENV, "/tmp/india_nlp_worker.py", "--from-date", frm, "--to-date", to, "--log", log]
    p = subprocess.Popen(cmd, stdout=open(log, 'w'), stderr=subprocess.STDOUT)
    procs.append(p)
    logger.info(f"NLP worker started: {frm}→{to} (PID {p.pid})")

for p in procs:
    p.wait()
logger.info("=== ALL NLP WORKERS DONE ===")
EOF

nohup $VENV /tmp/nlp_after_pdf.py >> /tmp/nlp_after_pdf.log 2>&1 &
echo "NLP monitor started: PID $! (auto-starts NLP when PDF workers finish)"

echo ""
echo "=== Pipeline resumed ==="
echo "Monitor: tail -f /tmp/pdf_w4.log /tmp/pdf_w5.log /tmp/pdf_w6.log"
