#!/bin/bash
# MIT adaptation; pinned reference and permission notice in REFERENCE.txt.
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

if [[ $# != 2 ]]; then
    echo "Usage: bash scripts/sign-package.sh PAYLOAD OUTPUT" >&2
    exit 1
fi
CONTEXT=$(python3 scripts/release_checks.py authorize)
IFS=$'\t' read -r PRODUCT RELEASE_BRANCH VERSION BUILD_DIR <<< "$CONTEXT"
PAYLOAD="$1"
OUTPUT="$2"
BUNDLE="$PAYLOAD/$PRODUCT.bundle"
python3 - "$PAYLOAD" "$OUTPUT" <<'PY'
import sys
sys.path.insert(0, "scripts")
import package
from pathlib import Path
payload, output = map(Path, sys.argv[1:])
package.no_symlink_path(payload)
package.no_symlink_path(output)
package.require(payload.is_dir() and not output.exists(), "Missing payload or existing output")
package.require(payload.absolute() not in output.absolute().parents, "Output must not be inside payload")
build = package.build_provenance(payload)
package.validate_payload_layout(payload, package.hashes(payload))
package.validate_payload_content(payload, build)
package.require(package.layout()["signature"] not in package.files_in(payload), "Payload already signed")
PY
mkdir -p "$OUTPUT"
ORIGINAL_UMASK=$(umask)
umask 077
SIGNING_TEMP=$(python3 - "$BUILD_DIR" <<'PY'
from pathlib import Path
import sys
import uuid
path = Path(sys.argv[1]) / (".signing-" + uuid.uuid4().hex)
path.mkdir(mode=0o700)
print(path.absolute())
PY
)
KEYCHAIN_PATH="$SIGNING_TEMP/signing.keychain-db"
CERT_PATH="$SIGNING_TEMP/certificate.p12"
KEYCHAIN_CREATED=0

cleanup() {
    status=$?
    trap - EXIT HUP INT TERM
    if [[ -f "$SIGNING_TEMP/search-list" ]]; then
        if ! python3 scripts/release_checks.py keychain-search-list --snapshot "$SIGNING_TEMP/search-list"; then
            echo "Could not restore the original user keychain search list" >&2
            status=1
        fi
    fi
    if [[ "$KEYCHAIN_CREATED" == 1 || -f "$KEYCHAIN_PATH" ]]; then
        if ! security delete-keychain "$KEYCHAIN_PATH"; then
            echo "Could not delete temporary signing keychain" >&2
            status=1
        fi
    fi
    if ! rm -f "$CERT_PATH" "$SIGNING_TEMP/identities" "$SIGNING_TEMP/identity" \
        "$SIGNING_TEMP/bundle-signature" "$SIGNING_TEMP/dmg-signature" "$SIGNING_TEMP/search-list"; then
        status=1
    fi
    if ! rmdir "$SIGNING_TEMP"; then
        echo "Could not remove temporary signing directory" >&2
        status=1
    fi
    if [[ "$status" != 0 ]]; then
        rm -f "$OUTPUT/SHA256SUMS" || status=1
    fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# create-keychain itself can register the keychain; capture before any mutation.
python3 scripts/release_checks.py keychain-search-list --snapshot "$SIGNING_TEMP/search-list" --capture
printf '%s' "$APPLE_DEVELOPER_ID_CERTIFICATE" | base64 --decode -o "$CERT_PATH"
security create-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
KEYCHAIN_CREATED=1
security set-keychain-settings -lut 21600 "$KEYCHAIN_PATH"
security unlock-keychain -p "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH"
python3 scripts/release_checks.py keychain-search-list \
    --snapshot "$SIGNING_TEMP/search-list" --add "$KEYCHAIN_PATH"
security import "$CERT_PATH" -P "$APPLE_DEVELOPER_ID_CERTIFICATE_PASSWORD" \
    -t cert -f pkcs12 -k "$KEYCHAIN_PATH" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s \
    -k "$KEYCHAIN_PASSWORD" "$KEYCHAIN_PATH" > /dev/null
security find-identity -v -p codesigning "$KEYCHAIN_PATH" > "$SIGNING_TEMP/identities"
python3 - "$SIGNING_TEMP/identities" "$SIGNING_TEMP/identity" <<'PY'
import os
from pathlib import Path
import re
import sys
team = os.environ["NOTARYTOOL_TEAM_ID"]
identities = re.findall(r'([A-Fa-f0-9]{40}) "Developer ID Application: [^"\n]+ \(' +
                        re.escape(team) + r'\)"', Path(sys.argv[1]).read_text())
if len(identities) != 1:
    raise SystemExit("Expected exactly one valid Developer ID Application identity for the configured team")
Path(sys.argv[2]).write_text(identities[0])
PY
IDENTITY=$(cat "$SIGNING_TEMP/identity")
NOTARY_PROFILE="$PRODUCT-release"
xcrun notarytool store-credentials "$NOTARY_PROFILE" --apple-id "$NOTARYTOOL_APPLE_ID" \
    --team-id "$NOTARYTOOL_TEAM_ID" --password "$NOTARYTOOL_PASSWORD" --keychain "$KEYCHAIN_PATH"
umask "$ORIGINAL_UMASK"

notarize() {
    local archive="$1"
    local format="$2"
    local report="$OUTPUT/$format-notarization.json"
    xcrun notarytool submit "$archive" --keychain-profile "$NOTARY_PROFILE" --keychain "$KEYCHAIN_PATH" \
        --wait --timeout 30m --output-format json > "$report"
    python3 scripts/release_checks.py notarization --report "$report"
}

codesign --force --timestamp --options runtime --keychain "$KEYCHAIN_PATH" --sign "$IDENTITY" "$BUNDLE"
codesign --verify --strict "$BUNDLE"
codesign --verify --strict "$BUNDLE/Contents/MacOS/$PRODUCT"
codesign --display --verbose=4 "$BUNDLE" 2> "$SIGNING_TEMP/bundle-signature"
python3 scripts/release_checks.py signature --details "$SIGNING_TEMP/bundle-signature" \
    --team "$NOTARYTOOL_TEAM_ID" --runtime
python3 scripts/package.py inventory --payload "$PAYLOAD" --output "$PAYLOAD/PROVENANCE.json" \
    --team "$NOTARYTOOL_TEAM_ID"
python3 scripts/package.py verify --payload "$PAYLOAD" --manifest "$PAYLOAD/PROVENANCE.json"
ZIP="$OUTPUT/$PRODUCT-$VERSION.zip"
DMG="$OUTPUT/$PRODUCT-$VERSION.dmg"
ditto -c -k --norsrc --noextattr "$PAYLOAD" "$ZIP"
notarize "$ZIP" zip
hdiutil create -format UDZO -volname "$PRODUCT" -srcfolder "$PAYLOAD" "$DMG"
codesign --force --timestamp --keychain "$KEYCHAIN_PATH" --sign "$IDENTITY" "$DMG"
codesign --verify --strict "$DMG"
codesign --display --verbose=4 "$DMG" 2> "$SIGNING_TEMP/dmg-signature"
python3 scripts/release_checks.py signature --details "$SIGNING_TEMP/dmg-signature" --team "$NOTARYTOOL_TEAM_ID"
notarize "$DMG" dmg
xcrun stapler staple "$DMG"
xcrun stapler validate "$DMG"
codesign --verify --strict "$DMG"
spctl --assess --type open --context context:primary-signature "$DMG"
cp "$PAYLOAD/PROVENANCE.json" "$OUTPUT/PROVENANCE.json"
python3 scripts/release_checks.py checksums --directory "$OUTPUT"
