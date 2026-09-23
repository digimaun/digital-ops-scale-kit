#!/usr/bin/env bash
set -euo pipefail
umask 077

bootstrap="$1"
root="$(mktemp -d)"
trap 'rm -rf -- "$root"' EXIT
export TEST_ROOT="$root" HOME="$root/home" XDG_DATA_HOME="$root/state"
export REAL_PYTHON="$(command -v python3)"
mkdir -p "$root/home" "$root/bin"
export PATH="$root/bin:/usr/bin:/bin"

"$REAL_PYTHON" - "$root/archive.zip" <<'PY'
import hashlib
import json
import sys
import zipfile

lock = b'lock-version = "1.0"\n'
manifest = {
    "apiVersion": "siteops.install/v1",
    "source": {
        "repository": "Azure/digital-ops-scale-kit",
        "commit": "c" * 40,
        "ref": "refs/heads/main",
    },
    "package": {"version": "1.0.0b1"},
    "files": [{
        "path": "pylock.toml", "size": len(lock),
        "sha256": hashlib.sha256(lock).hexdigest(),
    }],
}
with zipfile.ZipFile(sys.argv[1], "w") as archive:
    archive.writestr("bundle.json", json.dumps(manifest))
    archive.writestr("pylock.toml", lock)
PY
printf 'opaque proof\n' > "$root/proof.jsonl"
export TEST_ARCHIVE="$root/archive.zip" TEST_PROOF="$root/proof.jsonl"
export TEST_LOG="$root/pipx-calls" TEST_BIN_DIR="$root/bin"
export TEST_DOWNLOAD_LOG="$root/downloads"
: > "$TEST_LOG"
: > "$TEST_DOWNLOAD_LOG"

cat > "$root/bin/curl" <<'SH'
#!/usr/bin/env bash
output=""
for ((index=1; index<=$#; index++)); do
  if [[ "${!index}" == -o ]]; then next=$((index+1)); output="${!next}"; fi
done
[[ "$*" == *"https://github.com/Azure/digital-ops-scale-kit/releases/download/"* ]] || exit 99
printf '%s\n' "$*" >> "$TEST_DOWNLOAD_LOG"
if [[ "$*" == *"siteops-install.zip.attestation.jsonl" ]]; then
  cp "$TEST_PROOF" "$output"
elif [[ "$*" == *"siteops-install.zip" ]]; then
  cp "$TEST_ARCHIVE" "$output"
else
  exit 99
fi
SH
cat > "$root/bin/gh" <<'SH'
#!/usr/bin/env bash
case "$1 $2" in
  "version ") echo "gh version 2.95.0" ;;
  "attestation verify") printf '%s\n' "$TEST_VERIFY" ;;
  *) exit 99 ;;
esac
SH
cat > "$root/bin/python3" <<'SH'
#!/usr/bin/env bash
if [[ "$1 $2" == "-m venv" && "$3" == */backend-tools ]]; then
  mkdir -p "$3/bin"
  cat > "$3/bin/python" <<'PYTOOL'
#!/usr/bin/env bash
[[ "$1 $2" == "-m pip" && "$3" == download ]] || exit 99
[[ "${TEST_FAIL_PIP:-0}" != 1 ]] || exit 7
while (($#)); do
  if [[ "$1" == --dest ]]; then dest="$2"; shift 2; else shift; fi
done
printf 'controlled wheel\n' > "$dest/pip-26.2.1-py3-none-any.whl"
PYTOOL
  chmod +x "$3/bin/python"
  exit 0
fi
if [[ "$1 $2" == "-m venv" ]]; then mkdir -p "$3/bin"; exit 0; fi
exec "$REAL_PYTHON" "$@"
SH
cat > "$root/bin/sha256sum" <<'SH'
#!/usr/bin/env bash
if [[ "$1" == --check ]]; then
  read -r digest file
  if [[ "$file" == *pip-26.2.1-*.whl ]]; then
    [[ "$digest" == 71138adf1f4ca900cdb7d289c21b7494329f2332b6d85f0e1c42108c0384ed3e ]]
    exit
  fi
  printf '%s  %s\n' "$digest" "$file" | /usr/bin/sha256sum --check --status
  exit
fi
exec /usr/bin/sha256sum "$@"
SH
cat > "$root/bin/pipx" <<'SH'
#!/usr/bin/env bash
case "$1" in
  --version) echo 1.17.2 ;;
  list)
    if [[ -n "${TEST_LOCK_PATH:-}" ]]; then
      printf '{"venvs":{"siteops":{"metadata":{"main_package":{"lock_file":{"__Path__":"%s"}}}}}}\n' "$TEST_LOCK_PATH"
    else
      printf '{"venvs":{}}\n'
    fi ;;
  upgrade-shared|install|ensurepath)
    printf '%s\n' "$*" >> "$TEST_LOG" ;;
  runpip) echo 'pip 26.2.1 from controlled fixture' ;;
  environment) echo "$TEST_BIN_DIR" ;;
  *) exit 99 ;;
esac
SH
cat > "$root/bin/siteops" <<'SH'
#!/usr/bin/env bash
[[ "$1" == --version ]] || exit 99
echo 'siteops 1.0.0b1'
SH
cat > "$root/bin/sudo" <<'SH'
#!/usr/bin/env bash
echo "Unexpected OS installation in isolated test." >&2
exit 99
SH
chmod +x "$root/bin/"*

arguments=(--release siteops/v1.0.0b1 --source-commit "$(printf 'c%.0s' {1..40})" --yes)
export TEST_VERIFY=false
if bash "$bootstrap" "${arguments[@]}" > "$root/rejected.log" 2>&1; then
  echo "Invalid proof executed the installer." >&2
  exit 1
fi
[[ ! -d "$root/state/siteops/bundles" && ! -s "$TEST_LOG" ]] ||
  { echo "Invalid proof changed installation state." >&2; exit 1; }

export TEST_VERIFY=true TEST_FAIL_PIP=1
if bash "$bootstrap" "${arguments[@]}" > "$root/failed-feed.log" 2>&1; then
  echo "A missing backend wheel was accepted." >&2
  exit 1
fi
bundle="$root/state/siteops/bundles/$(/usr/bin/sha256sum "$TEST_ARCHIVE" | cut -d ' ' -f 1)"
[[ -f "$bundle/pylock.toml" ]] ||
  { cat "$root/failed-feed.log" >&2;
    echo "Controlled partial bundle was not retained." >&2; exit 1; }
export TEST_FAIL_PIP=0
bash "$bootstrap" "${arguments[@]}" > "$root/recovered.log" 2>&1 ||
  { cat "$root/recovered.log" >&2; exit 1; }
grep -q 'install siteops --lock' "$TEST_LOG"

export TEST_LOCK_PATH="$bundle/pylock.toml"
before="$(wc -l < "$TEST_LOG")"
downloads_before="$(wc -l < "$TEST_DOWNLOAD_LOG")"
bash "$bootstrap" "${arguments[@]}" > "$root/repeated.log" 2>&1 ||
  { cat "$root/repeated.log" >&2; exit 1; }
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "Repeated install changed pipx tooling." >&2; exit 1; }
[[ "$(wc -l < "$TEST_DOWNLOAD_LOG")" == "$downloads_before" ]] ||
  { echo "Repeated install downloaded the same authenticated release." >&2; exit 1; }

export TEST_LOCK_PATH="/other/pylock.toml"
if bash "$bootstrap" "${arguments[@]}" > "$root/foreign.log" 2>&1; then
  echo "Another installed build was replaced implicitly." >&2
  exit 1
fi
[[ "$(wc -l < "$TEST_LOG")" == "$before" ]] ||
  { echo "Rejected replacement changed pipx tooling." >&2; exit 1; }
bash "$bootstrap" "${arguments[@]}" --replace > "$root/replaced.log" 2>&1 ||
  { cat "$root/replaced.log" >&2; exit 1; }
grep -q 'install siteops --lock .* --force' "$TEST_LOG"
echo 'Bootstrap control flow passed with isolated tools and no network.'
