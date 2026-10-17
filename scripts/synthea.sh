#!/usr/bin/env bash
# Generate a synthetic FHIR R4 cohort with a pinned Synthea release and a fixed seed.
# Usage: scripts/synthea.sh [patients] [seed] [out_dir]      (defaults: 2000 42 data/synthea)
# Needs Java 11+. Output: <out_dir>/fhir/<one transaction bundle per patient>.json
set -euo pipefail
cd "$(dirname "$0")/.."
PATIENTS="${1:-${CFP_PATIENTS:-2000}}"
SEED="${2:-42}"
OUT="${3:-data/synthea}"
VERSION=v4.0.0
SHA256=ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1
JAR=.synthea/synthea-with-dependencies.jar

mkdir -p .synthea "$(dirname "$OUT")"
if [ ! -f "$JAR" ]; then
  echo "downloading Synthea $VERSION"
  curl -sSL -o "$JAR" "https://github.com/synthetichealth/synthea/releases/download/$VERSION/synthea-with-dependencies.jar"
fi
echo "$SHA256  $JAR" | sha256sum -c - >/dev/null

rm -rf "$OUT"
# -r and -e pin the reference date and the end of the simulation, so the same seed gives the same cohort on
# any day; 10 years of history per patient; no hospital or practitioner bundles (not needed here).
java -jar "$JAR" -p "$PATIENTS" -s "$SEED" -cs "$SEED" -r 20250101 -e 20250101 \
  --exporter.baseDirectory="$OUT" \
  --exporter.fhir.export=true \
  --exporter.fhir.use_us_core_ig=false \
  --exporter.hospital.fhir.export=false \
  --exporter.practitioner.fhir.export=false \
  --exporter.years_of_history=10 \
  --exporter.csv.export=false \
  --exporter.x12.export=false \
  "--exporter.fhir.excluded_resources=Claim,ExplanationOfBenefit,Provenance,SupplyDelivery" \
  Florida Tampa >"$OUT.log" 2>&1
echo "bundles: $(ls "$OUT/fhir" | wc -l)  size: $(du -sh "$OUT/fhir" | cut -f1)  log: $OUT.log"
