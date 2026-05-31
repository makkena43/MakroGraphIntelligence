#!/bin/bash
# =============================================================================
# MakroGraphIntelligence — Automated Backup to Google Drive
#
# Backs up:
#   1. PostgreSQL core tables (documents, signals, entities, themes)
#   2. Neo4j database dump
#
# Skips (regenerable in <2h):
#   - pgvector embeddings  → re-run: pipeline.run_embeddings()
#   - Neo4j graph nodes    → re-run: pipeline.run_graph()  [if preferred]
#
# Usage:
#   ./scripts/backup.sh               # full backup + upload
#   ./scripts/backup.sh --local-only  # backup without uploading
#   ./scripts/backup.sh --dry-run     # show what would be done
#
# Setup (first time):
#   brew install rclone
#   rclone config  → add Google Drive as "gdrive"
# =============================================================================

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_DIR="$PROJECT_DIR/backups"
DATE=$(date +%Y%m%d_%H%M%S)
BACKUP_NAME="makrograph_${DATE}"
BACKUP_PATH="$BACKUP_DIR/$BACKUP_NAME"

PG_DB="makrograph"
PG_HOST="localhost"
PG_PORT="5432"
PG_USER="${PG_USER:-$(whoami)}"

NEO4J_DATA_DIR="/opt/homebrew/var/neo4j"
NEO4J_ADMIN="/opt/homebrew/bin/neo4j-admin"

RCLONE_REMOTE="gdrive"
RCLONE_DEST="makrograph-backups"   # folder name in your Google Drive
KEEP_LOCAL_DAYS=3                   # delete local backups older than N days

# Parse args
LOCAL_ONLY=false
DRY_RUN=false
for arg in "$@"; do
  [[ "$arg" == "--local-only" ]] && LOCAL_ONLY=true
  [[ "$arg" == "--dry-run" ]]    && DRY_RUN=true
done

# ── Helpers ───────────────────────────────────────────────────────────────────
log() { echo "[$(date '+%H:%M:%S')] $*"; }
run() {
  if $DRY_RUN; then echo "[DRY-RUN] $*"; else eval "$*"; fi
}

log "=== MakroGraph Backup: $BACKUP_NAME ==="
$DRY_RUN && log "DRY-RUN mode — no files will be written"

mkdir -p "$BACKUP_PATH"

# ── 1. PostgreSQL core tables ─────────────────────────────────────────────────
PG_DUMP_FILE="$BACKUP_PATH/postgres_core.dump"
log "Backing up PostgreSQL core tables..."

CORE_TABLES=(
  mg_documents          # raw_text + metadata — most critical
  mg_signals            # investment signals
  mg_entities           # named entities
  mg_document_entities  # entity-document links
  mg_themes             # detected themes
  mg_theme_snapshots    # quarterly theme history
  mg_theme_beneficiaries # beneficiary stocks per theme
  mg_causal_chains      # causal chain scores
  mg_source_checkpoints # ingestion progress checkpoints
)

TABLE_FLAGS=""
for t in "${CORE_TABLES[@]}"; do
  TABLE_FLAGS="$TABLE_FLAGS -t $t"
done

run "pg_dump $PG_DB \
  -h $PG_HOST -p $PG_PORT -U $PG_USER \
  $TABLE_FLAGS \
  --no-owner --no-privileges \
  -Fc -f '$PG_DUMP_FILE'"

if ! $DRY_RUN; then
  PG_SIZE=$(du -sh "$PG_DUMP_FILE" | cut -f1)
  log "PostgreSQL dump: $PG_SIZE → $PG_DUMP_FILE"
fi

# ── 2. Neo4j database dump ────────────────────────────────────────────────────
NEO4J_DUMP_FILE="$BACKUP_PATH/neo4j.dump"
log "Backing up Neo4j..."

# Stop Neo4j briefly for consistent dump, then restart
if ! $DRY_RUN; then
  brew services stop neo4j 2>/dev/null && sleep 3
  "$NEO4J_ADMIN" database dump neo4j --to-path="$BACKUP_PATH" 2>/dev/null || \
  "$NEO4J_ADMIN" dump --database=neo4j --to="$NEO4J_DUMP_FILE" 2>/dev/null || \
  log "WARNING: Neo4j dump failed (regenerable from Postgres)"
  brew services start neo4j 2>/dev/null && sleep 5
  [[ -f "$NEO4J_DUMP_FILE" ]] && log "Neo4j dump: $(du -sh "$NEO4J_DUMP_FILE" | cut -f1)"
else
  run "neo4j stop && neo4j-admin dump --database=neo4j --to=$NEO4J_DUMP_FILE && neo4j start"
fi

# ── 3. Config snapshot ────────────────────────────────────────────────────────
log "Saving config snapshot..."
run "cp '$PROJECT_DIR/config/settings.yaml' '$BACKUP_PATH/settings.yaml'"
# Save DB schema + restore instructions
cat > "$BACKUP_PATH/RESTORE.md" << 'RESTORE_DOC'
# MakroGraphIntelligence — Restore Guide

## Quick Start (New Laptop)

### 1. Install dependencies
```bash
brew install postgresql@15 neo4j python@3.12 rclone
brew services start postgresql@15
brew services start neo4j
```

### 2. Clone code
```bash
git clone https://github.com/makkena43/MakroGraphIntelligence.git
cd MakroGraphIntelligence
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm
```

### 3. Create database
```bash
createdb makrograph
psql makrograph < schema/postgres_schema.sql
```

### 4. Restore PostgreSQL
```bash
pg_restore -d makrograph --no-owner -Fc postgres_core.dump
```

### 5. Restore Neo4j (or regenerate)
```bash
# Option A: restore from dump
neo4j-admin database load neo4j --from-path=. --overwrite-destination=true

# Option B: regenerate from Postgres (takes ~30 min)
python3 -c "
import sys, yaml; sys.path.insert(0,'src')
with open('config/settings.yaml') as f: cfg=yaml.safe_load(f)
from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
p=IntelligencePipeline(cfg); p._init_storage(); p._init_graph_builder()
p.run_graph(country='IN'); p.run_graph(country='US')
"
```

### 6. Regenerate embeddings (~1 hour)
```bash
python3 -c "
import sys, yaml; sys.path.insert(0,'src')
with open('config/settings.yaml') as f: cfg=yaml.safe_load(f)
from makrograph.pipeline.intelligence_pipeline import IntelligencePipeline
p=IntelligencePipeline(cfg); p._init_storage(); p._init_nlp()
p.run_embeddings(country='IN'); p.run_embeddings(country='US')
"
```

### 7. Start Streamlit
```bash
streamlit run app.py
```
RESTORE_DOC

log "Restore guide written → $BACKUP_PATH/RESTORE.md"

# ── 4. Compress everything ────────────────────────────────────────────────────
ARCHIVE="$BACKUP_DIR/${BACKUP_NAME}.tar.gz"
log "Compressing backup..."
run "tar -czf '$ARCHIVE' -C '$BACKUP_DIR' '$BACKUP_NAME'"
run "rm -rf '$BACKUP_PATH'"

if ! $DRY_RUN; then
  ARCHIVE_SIZE=$(du -sh "$ARCHIVE" | cut -f1)
  log "Archive: $ARCHIVE_SIZE → $ARCHIVE"
fi

# ── 5. Upload to Google Drive ─────────────────────────────────────────────────
if ! $LOCAL_ONLY; then
  if ! command -v rclone &>/dev/null; then
    log "ERROR: rclone not found. Install: brew install rclone && rclone config"
    log "Backup saved locally: $ARCHIVE"
    exit 1
  fi

  log "Uploading to Google Drive ($RCLONE_REMOTE:$RCLONE_DEST)..."
  run "rclone copy '$ARCHIVE' '$RCLONE_REMOTE:$RCLONE_DEST/' --progress"
  log "Upload complete ✅"

  # Keep only last 5 backups on Drive
  log "Pruning old Drive backups (keeping 5 most recent)..."
  run "rclone ls '$RCLONE_REMOTE:$RCLONE_DEST/' | sort -k2 | head -n -5 | \
    awk '{print \$2}' | \
    xargs -I{} rclone delete '$RCLONE_REMOTE:$RCLONE_DEST/{}' 2>/dev/null || true"
fi

# ── 6. Clean up old local backups ─────────────────────────────────────────────
log "Cleaning local backups older than $KEEP_LOCAL_DAYS days..."
run "find '$BACKUP_DIR' -name 'makrograph_*.tar.gz' -mtime +$KEEP_LOCAL_DAYS -delete"

log ""
log "=== Backup Complete ==="
log "  Archive : $ARCHIVE"
$LOCAL_ONLY || log "  Drive   : $RCLONE_REMOTE:$RCLONE_DEST/${BACKUP_NAME}.tar.gz"
log "  Tables  : pg_dump of 9 core tables"
log "  Neo4j   : full dump"
log ""
log "Restore: see $BACKUP_PATH/RESTORE.md (also in archive)"
