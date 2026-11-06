#!/bin/zsh
# User-run helper; agents must not execute it with real credential tools.
# Canonical guidance: ../.github/instructions/plugin-dev.instructions.md
set -euo pipefail

SERVICE=4d-plugin-release
NAMES=(
  APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD
  KEYCHAIN_PASSWORD
  NOTARYTOOL_APPLE_ID
  NOTARYTOOL_PASSWORD
  NOTARYTOOL_TEAM_ID
)

if (( $# == 0 )); then
  print -u2 "usage: $0 owner/repo [owner/repo ...]"
  exit 1
fi

for tool in gh security openssl base64 tr mktemp rm; do
  command -v "$tool" >/dev/null || { print -u2 "$tool is not installed"; exit 1; }
done
gh auth status >/dev/null 2>&1 || { print -u2 "run: gh auth login"; exit 1; }

kc_get() {
  local code=0
  security find-generic-password -s "$SERVICE" -a "$1" -w 2>/dev/null || code=$?
  if (( code != 0 && code != 44 )); then
    print -u2 "Keychain lookup failed for $1 (status $code); check login Keychain access."
  fi
  return "$code"
}

if [[ -n "${CERT_P12:-}" ]]; then
  [[ -f "$CERT_P12" ]] || { print -u2 "not found: $CERT_P12"; exit 1; }
  certificate=$(base64 -i "$CERT_P12" | tr -d '\n')
  security add-generic-password -U -s "$SERVICE" -a APPLE_DEVELOPER_ID_CERTIFICATE \
    -w "$certificate"
  print "stored APPLE_DEVELOPER_ID_CERTIFICATE from $CERT_P12"
fi
kc_get APPLE_DEVELOPER_ID_CERTIFICATE >/dev/null || {
  print -u2 "no certificate available: check login Keychain access or rerun with CERT_P12=/path/to/DeveloperID.p12"
  exit 1
}

for name in $NAMES; do
  if kc_get "$name" >/dev/null; then
    continue
  else
    code=$?
    (( code == 44 )) || exit "$code"
    print "enter $name (typed twice, hidden):"
    security add-generic-password -U -s "$SERVICE" -a "$name" -w
  fi
done

umask 077
P12_TMP=$(mktemp -t release-cert)
trap 'rm -f "$P12_TMP"' EXIT
kc_get APPLE_DEVELOPER_ID_CERTIFICATE | base64 -D > "$P12_TMP"
p12_subject() {
  kc_get APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD \
    | openssl pkcs12 -in "$P12_TMP" -nokeys -passin stdin "$@" 2>/dev/null \
    | openssl x509 -noout -subject -enddate 2>/dev/null
}
if ! p12_subject && ! p12_subject -legacy; then
  print -u2 "the stored certificate cannot be opened with the stored password."
  print -u2 "check the certificate/password pair and OpenSSL legacy-encryption support."
  print -u2 "replace the wrong one, then rerun:"
  print -u2 "  security delete-generic-password -s $SERVICE -a APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD"
  print -u2 "  CERT_P12=/path/to/the/right.p12 $0 $*"
  exit 1
fi
read -q "?is this the Developer ID Application certificate to ship with? [y/N] " || { print; exit 1; }
print

for repo in "$@"; do
  print "== $repo"
  for name in APPLE_DEVELOPER_ID_CERTIFICATE $NAMES; do
    value=$(kc_get "$name")
    [[ -n "$value" ]] || { print -u2 "empty value for $name"; exit 1; }
    print -rn -- "$value" | gh secret set "$name" --repo "$repo"
  done
  gh secret list --repo "$repo"
done
