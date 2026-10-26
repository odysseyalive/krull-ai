#!/usr/bin/env bash
set -euo pipefail

# scripts/merge-env.sh
# Merge keys from .env.sample into .env without overwriting existing values.
# Preserves comments and ordering for existing .env; appends missing keys
# (including their comment block) to the end of .env.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SAMPLE="$PROJECT_DIR/.env.sample"
TARGET="$PROJECT_DIR/.env"
TMP="$PROJECT_DIR/.env.merged"

if [ ! -f "$SAMPLE" ]; then
  echo "No .env.sample found; nothing to merge."
  exit 0
fi

# Ensure target exists
if [ ! -f "$TARGET" ]; then
  echo "Creating new .env from sample"
  cp "$SAMPLE" "$TARGET"
  exit 0
fi

python3 - <<PY
import re,sys
samp='$SAMPLE'
tgt='$TARGET'
out='$TMP'

with open(samp) as f:
    s_lines=f.readlines()
with open(tgt) as f:
    t_lines=f.readlines()

# Find keys present in target
key_re=re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=')
present=set()
for l in t_lines:
    m=key_re.match(l)
    if m:
        present.add(m.group(1))

# Walk sample and capture blocks (comments + key line)
blocks=[]
cur=[]
for l in s_lines:
    if key_re.match(l):
        # include this key line with any preceding comments (cur)
        cur.append(l)
        blocks.append(''.join(cur))
        cur=[]
    else:
        cur.append(l)
# if trailing comments
if cur:
    blocks.append(''.join(cur))

# For each block, detect key and append if missing
append_blocks=[]
for blk in blocks:
    # The key line is the block's last line; '^' without re.M would only
    # see the first line, missing every key that has a comment above it.
    m=key_re.match(blk.splitlines()[-1])
    if m:
        k=m.group(1)
        if k not in present:
            append_blocks.append(blk)
    else:
        # non-key comments at end — ignore
        pass

# Nothing to add: leave .env untouched (same inode, owner, mode, mtime).
# The krull-updater container runs this as root, so rewriting here would
# hand the user's .env to root on every Update-button run.
if not append_blocks:
    print('No missing keys; %s unchanged' % tgt)
    sys.exit(0)

# Write merged file: original target followed by appended blocks.
# Created 0600 so a private .env is never briefly world-readable, then
# given the original owner/group/mode before it replaces the target.
import os,stat
st=os.stat(tgt)
with os.fdopen(os.open(out,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as f:
    f.writelines(t_lines)
    f.write('\n# --- Appended from .env.sample ---\n')
    for b in append_blocks:
        f.write(b)
try:
    os.chown(out,st.st_uid,st.st_gid)
except PermissionError:
    # Non-root caller on a file it does not own: keep the caller's ids.
    pass
os.chmod(out,stat.S_IMODE(st.st_mode))

# Replace target
os.replace(out,tgt)
print('Merged %d missing keys into %s' % (len(append_blocks), tgt))
PY
