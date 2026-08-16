#!/usr/bin/env bash
set -euo pipefail

# This is the narrow host acquisition/bootstrap TCB. It authenticates the
# complete upstream Python bin+lib tree before any recovery Python is started.
python_url="https://github.com/actions/python-versions/releases/download/3.11.14-18393181605/python-3.11.14-linux-24.04-x64.tar.gz"
python_archive_sha256="295c25eeb4fdad1ec9526a27fbd9b476d7c79b00547d74d809b306381d0796d5"
python_runtime_tree_sha256="4180c03100ad4a58d4786eb10c3ba2cb3ac88dc5a30f7100410afef6b1e5ab2f"

test "$#" -eq 1
destination="$1"
test ! -e /etc/ld.so.preload
archive="$destination/python.tar.gz"
runtime="$destination/runtime"
runtime_tree_inventory="$destination/runtime-tree.inventory"

write_runtime_inventory() {
  output_path="$1"
  (
    cd "$runtime"
    while IFS= read -r -d '' path; do
      relative="${path#./}"
      if test -L "$path"; then
        printf 'link\t%s\t%s\n' "$relative" "$(readlink -- "$path")"
      elif test -f "$path"; then
        printf 'file\t%s\t%s\t%s\t%s\n' \
          "$(stat -c '%a' -- "$path")" \
          "$(stat -c '%s' -- "$path")" \
          "$(sha256sum "$path" | cut -d ' ' -f 1)" \
          "$relative"
      else
        exit 1
      fi
    done < <(
      find ./bin/python3.11 ./lib \( -type f -o -type l \) -print0 | LC_ALL=C sort -z
    )
  ) > "$output_path"
}

verify_exact_runtime() {
  test -f "$archive"
  test ! -L "$archive"
  test "$(stat -c '%s' -- "$archive")" = "118903216"
  test "$(sha256sum "$archive" | cut -d ' ' -f 1)" = "$python_archive_sha256"
  test -d "$runtime"
  test ! -L "$runtime"
  test -f "$runtime_tree_inventory"
  test ! -L "$runtime_tree_inventory"
  test "$(sha256sum "$runtime_tree_inventory" | cut -d ' ' -f 1)" = \
    "$python_runtime_tree_sha256"
  test -f "$runtime/bin/python3.11"
  test ! -L "$runtime/bin/python3.11"
  temporary_inventory="$(mktemp "${TMPDIR:-/tmp}/kestrel-recovery-runtime.XXXXXX")"
  expected_content_inventory="$(mktemp "${TMPDIR:-/tmp}/kestrel-recovery-expected.XXXXXX")"
  observed_content_inventory="$(mktemp "${TMPDIR:-/tmp}/kestrel-recovery-observed.XXXXXX")"
  trap 'rm -f -- "$temporary_inventory" "$expected_content_inventory" "$observed_content_inventory"' EXIT
  write_runtime_inventory "$temporary_inventory"
  awk -F '\t' 'BEGIN {OFS="\t"} $1 == "file" {print $1,$3,$4,$5; next} {print}' \
    "$runtime_tree_inventory" > "$expected_content_inventory"
  awk -F '\t' 'BEGIN {OFS="\t"} $1 == "file" {print $1,$3,$4,$5; next} {print}' \
    "$temporary_inventory" > "$observed_content_inventory"
  cmp --silent -- "$expected_content_inventory" "$observed_content_inventory"
  test "$(stat -c '%a' -- "$runtime/bin/python3.11")" = "500"
  while IFS= read -r -d '' runtime_file; do
    test "$(stat -c '%a' -- "$runtime_file")" = "444"
  done < <(find "$runtime/lib" -type f -print0)
  while IFS= read -r -d '' runtime_directory; do
    test "$(stat -c '%a' -- "$runtime_directory")" = "555"
  done < <(find "$runtime/bin" "$runtime/lib" -type d -print0)
  test "$(sha256sum "$runtime/bin/python3.11" | cut -d ' ' -f 1)" = \
    "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
  rm -f -- "$temporary_inventory" "$expected_content_inventory" "$observed_content_inventory"
  trap - EXIT
}

if test -e "$destination" || test -L "$destination"; then
  test -d "$destination"
  test ! -L "$destination"
  verify_exact_runtime
  printf '%s\n' "$runtime/bin/python3.11"
  exit 0
fi

mkdir -m 0700 "$destination"

curl --proto '=https' --tlsv1.2 --fail --location --silent --show-error \
  "$python_url" --output "$archive"
test "$(stat -c '%s' -- "$archive")" = "118903216"
test "$(sha256sum "$archive" | cut -d ' ' -f 1)" = "$python_archive_sha256"
mkdir -m 0700 "$runtime"
tar --extract --gzip --file "$archive" --directory "$runtime" --no-same-owner
test -f "$runtime/bin/python3.11"
test ! -L "$runtime/bin/python3.11"

write_runtime_inventory "$runtime_tree_inventory"
test "$(sha256sum "$runtime_tree_inventory" | cut -d ' ' -f 1)" = \
  "$python_runtime_tree_sha256"
test "$(sha256sum "$runtime/bin/python3.11" | cut -d ' ' -f 1)" = \
  "dcd2d22a91c5adb37fa3f54a3a16d2ed7616b84931eb606c0fdfbca38395dab8"
find "$runtime/lib" -type f -exec chmod 0444 {} +
find "$runtime/bin" "$runtime/lib" -type d -exec chmod 0555 {} +
chmod 0500 "$runtime/bin/python3.11"
verify_exact_runtime
printf '%s\n' "$runtime/bin/python3.11"
