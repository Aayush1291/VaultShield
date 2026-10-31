#!/bin/sh
# Render the Graphviz diagrams (*.dot) to 300 dpi PNGs for the report.
# Usage: sh docs/blackbook/diagrams/render_dot.sh <out_dir> [stem ...]
# Requires Graphviz (brew install graphviz). Each .dot file is drawn at its
# printed size, so the PNG's width in inches at 300 dpi is its width on paper.
set -e
here=$(cd "$(dirname "$0")" && pwd)
out=${1:?output directory}
shift
mkdir -p "$out"
if [ "$#" -eq 0 ]; then
  set -- $(cd "$here" && ls *.dot | sed 's/\.dot$//')
fi
for stem in "$@"; do
  dot -Tpng -Gdpi=300 "$here/$stem.dot" -o "$out/$stem.png"
  size=$(dot -Tplain "$here/$stem.dot" | head -1 | awk '{printf "%.2f x %.2f in", $3, $4}')
  echo "$stem: $size"
done
