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
    m=key_re.search(blk)
    if m:
        k=m.group(1)
        if k not in present:
            append_blocks.append(blk)
    else:
        # non-key comments at end — ignore
        pass

# Write merged file: original target followed by appended blocks
with open(out,'w') as f:
    f.writelines(t_lines)
    if append_blocks:
        f.write('\n# --- Appended from .env.sample ---\n')
        for b in append_blocks:
            f.write(b)

# Replace target
import os
os.replace(out,tgt)
print('Merged %d missing keys into %s' % (len(append_blocks), tgt))
PY
