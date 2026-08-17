#!/usr/bin/env bash
#
# Print the URL of the revision carrying a given Cloud Run traffic tag.
#
#   ./scripts/candidate_url.sh coach-api us-central1 [tag]
#
# Used by .github/workflows/deploy-cloudrun.yml between deploying a candidate revision
# and smoke-testing it. It is a file rather than inline YAML for the same reason
# `smoke.sh` is: inline, the only way to test it is to merge and watch, and this
# particular read has now failed twice in exactly that way.
#
# Two things gcloud will not do here, both learned the hard way:
#
#   * `--filter` is a *list*-command flag. `describe` returns one resource and rejects it:
#     "unrecognized arguments: --filter".
#   * a `value()` projection selecting across a repeated field returns a list, which
#     gcloud renders as its literal repr — ['https://…'], brackets and quotes included.
#
# So the narrowing happens here. `--flatten` turns each traffic target into its own
# record, and `csv[no-heading]` gives an unambiguous separator: `value()` with several
# fields separates them with a tab, which is easy to assume and awkward to verify, while
# CSV is defined and keeps empty fields as empty. Untagged targets have no tag, and their
# empty first column must survive the split or the URL shifts a column left.
set -euo pipefail

SERVICE="${1:-coach-api}"
REGION="${2:-us-central1}"
TAG="${3:-candidate}"

traffic="$(gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --flatten="status.traffic[]" \
  --format="csv[no-heading](status.traffic.tag,status.traffic.url)")"

url="$(printf '%s\n' "$traffic" | awk -F, -v tag="$TAG" '$1 == tag { print $2; exit }')"

if [ -z "$url" ]; then
  {
    echo "No revision is carrying the '${TAG}' tag on service '${SERVICE}' in ${REGION}."
    echo "Traffic targets Cloud Run reported:"
    printf '%s\n' "$traffic" | sed 's/^/  /'
  } >&2
  exit 1
fi

printf '%s\n' "$url"
