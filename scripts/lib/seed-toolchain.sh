# The seed bootstrap's host and native linker boundary. No Axiom compiler,
# network or repository history is read here. The caller sources
# imports.sh first, then selects its host and prepares one link directory.

seed_host_target() {
  local system="$1" machine="$2"
  case "$system" in
    Darwin) seed_os=darwin ;;
    Linux) seed_os=linux ;;
    FreeBSD) seed_os=freebsd ;;
    MINGW*|MSYS*|CYGWIN*|Windows_NT) seed_os=windows ;;
    *) echo "unsupported OS $system" >&2; return 1 ;;
  esac
  # Git Bash and its PROCESSOR_* variables can describe an emulated x64
  # process on Windows ARM64. Ask Windows for the native machine instead.
  if [[ "$seed_os" == windows ]]; then
    machine="$(seed_windows_native_arch "$machine")" || return 1
  fi
  case "$machine" in
    arm64|aarch64|ARM64) seed_arch=aarch64 ;;
    x86_64|amd64|AMD64) seed_arch=x86_64 ;;
    *) echo "unsupported architecture $machine" >&2; return 1 ;;
  esac
  seed_target="$seed_os-$seed_arch"
  seed_exe_suffix=""
  [[ "$seed_os" != windows ]] || seed_exe_suffix=.exe
  [[ "$seed_os" != windows ]] || echo "Windows native host: $seed_target (shell machine: $2)" >&2
}

seed_windows_machine() {
  case "$1" in
    windows-x86_64) seed_dll_machine=i386:x86-64; seed_link_machine=x64; seed_pe_machine=IMAGE_FILE_MACHINE_AMD64; seed_triple=x86_64-pc-windows-msvc ;;
    windows-aarch64) seed_dll_machine=arm64; seed_link_machine=arm64; seed_pe_machine=IMAGE_FILE_MACHINE_ARM64; seed_triple=aarch64-pc-windows-msvc ;;
    *) echo "not a Windows seed target: $1" >&2; return 1 ;;
  esac
}

seed_windows_host_ir() { # <triple>: a probe executable for the shell's machine
  cat <<EOF
target triple = "$1"
declare dllimport ptr @GetCurrentProcess()
declare dllimport i32 @IsWow64Process2(ptr, ptr, ptr)
declare dllimport void @ExitProcess(i32) noreturn
define void @mainCRTStartup() {
  %process = alloca i16, align 2
  %native = alloca i16, align 2
  %handle = call ptr @GetCurrentProcess()
  %ok = call i32 @IsWow64Process2(ptr %handle, ptr %process, ptr %native)
  %success = icmp ne i32 %ok, 0
  br i1 %success, label %machine, label %failed
machine:
  %value = load i16, ptr %native, align 2
  switch i16 %value, label %unsupported [i16 34404, label %x64
                                        i16 43620, label %arm64]
x64:
  call void @ExitProcess(i32 10)
  unreachable
arm64:
  call void @ExitProcess(i32 11)
  unreachable
failed:
  call void @ExitProcess(i32 12)
  unreachable
unsupported:
  call void @ExitProcess(i32 13)
  unreachable
}
EOF
}

seed_windows_build_host_probe() { # <shell-machine> <private-work-dir>
  local machine="$1" dir="$2" tool imports expected
  case "$machine" in
    arm64|aarch64|ARM64) seed_windows_machine windows-aarch64 ;;
    x86_64|amd64|AMD64) seed_windows_machine windows-x86_64 ;;
    *) echo "unsupported Windows shell architecture $machine" >&2; return 1 ;;
  esac
  for tool in llc llvm-dlltool lld-link llvm-readobj; do
    command -v "$tool" >/dev/null || { echo "$tool is not on PATH (Windows host detection needs LLVM)" >&2; return 1; }
  done
  mkdir -p "$dir" || return 1
  seed_windows_host_ir "$seed_triple" > "$dir/native-host.ll" || return 1
  # This probe has its own three-import boundary. It does not change the
  # compiler's reviewed import allowlist or require a Windows SDK.
  printf 'LIBRARY kernel32.dll\nEXPORTS\nGetCurrentProcess\nIsWow64Process2\nExitProcess\n' > "$dir/kernel32.def"
  llvm-dlltool -m "$seed_dll_machine" -d "$dir/kernel32.def" -l "$dir/kernel32.lib" || return 1
  llc -filetype=obj -relocation-model=pic "$dir/native-host.ll" -o "$dir/native-host.obj" || return 1
  lld-link -subsystem:console -entry:mainCRTStartup "-machine:$seed_link_machine" \
    -timestamp:0 -Brepro "-out:$dir/native-host.exe" "$dir/native-host.obj" "$dir/kernel32.lib" || return 1
  llvm-readobj --file-headers "$dir/native-host.exe" | grep -E "Machine: $seed_pe_machine([[:space:](]|$)" >/dev/null \
    || { echo "Windows host probe is not a $seed_pe_machine image" >&2; return 1; }
  llvm-readobj --coff-imports "$dir/native-host.exe" >/dev/null || return 1
  imports="$(pe_imports_by_dll "$dir/native-host.exe")"
  expected="$(printf '%s\n' ExitProcess@kernel32 GetCurrentProcess@kernel32 IsWow64Process2@kernel32 | LC_ALL=C sort)"
  [[ "$imports" == "$expected" ]] || { echo "Windows host probe has unexpected imports: $imports" >&2; return 1; }
}

seed_windows_native_arch() ( # <shell-machine>; subprocess owns its temporary files
  local dir status
  dir="$(mktemp -d)" || return 1
  trap 'rm -rf "$dir"' EXIT
  seed_windows_build_host_probe "$1" "$dir" >&2 || return 1
  if "$dir/native-host.exe"; then status=0; else status=$?; fi
  case "$status" in
    10) printf '%s\n' x86_64 ;;
    11) printf '%s\n' aarch64 ;;
    12) echo "IsWow64Process2 could not determine the native Windows machine" >&2; return 1 ;;
    13) echo "IsWow64Process2 reported an unsupported native Windows machine" >&2; return 1 ;;
    *) echo "Windows host probe failed (exit $status); refusing to infer the native machine from process variables" >&2; return 1 ;;
  esac
)

seed_prepare_link() { # <target> <work-dir> <Windows allowlist>
  local target="$1" dir="$2" dll
  seed_link_args=()
  case "$target" in
    darwin-*) seed_link_args=(-e _main) ;;
    windows-*)
      seed_windows_machine "$target" || return 1
      seed_windows_allow="$3"
      mkdir -p "$dir" || return 1
      while IFS= read -r dll; do
        { printf 'LIBRARY %s.dll\nEXPORTS\n' "$dll"; permitted_windows_for_dll "$3" "$dll"; } > "$dir/$dll.def"
        llvm-dlltool -m "$seed_dll_machine" -d "$dir/$dll.def" -l "$dir/$dll.lib" || return 1
        seed_link_args+=("$dir/$dll.lib")
      done < <(permitted_windows_dlls "$3")
      [[ ${#seed_link_args[@]} -gt 0 ]] || { echo "the Windows import allowlist names no DLLs" >&2; return 1; }
      ;;
  esac
}

seed_validate_windows() { # <exe>
  local exe="$1" names unexpected misplaced count
  llvm-readobj --file-headers "$exe" | grep "Machine: $seed_pe_machine" >/dev/null \
    || { echo "$exe is not a $seed_pe_machine image" >&2; return 1; }
  llvm-readobj --coff-imports "$exe" >/dev/null \
    || { echo "could not read the PE imports of $exe" >&2; return 1; }
  names="$(imports_of "$exe")" || return 1
  count="$(printf '%s\n' "$names" | grep -c . || true)"
  # The bootstrap compiler's runtime needs at least four imports. An
  # empty or broken PE reader must not pass as an import-free compiler.
  (( count >= 4 )) || { echo "$exe exposes only $count imports; the PE reader did not read the compiler" >&2; return 1; }
  unexpected="$(LC_ALL=C comm -23 <(printf '%s\n' "$names" | LC_ALL=C sort -u) <(permitted_windows "$seed_windows_allow"))"
  [[ -z "$unexpected" ]] || { echo "$exe imports unpermitted names: $unexpected" >&2; return 1; }
  misplaced="$(misplaced_imports "$exe" "$seed_windows_allow")"
  [[ -z "$misplaced" ]] || { echo "$exe imports names from an unpermitted DLL: $misplaced" >&2; return 1; }
  if pe_imports_by_dll "$exe" | grep -E "@($crt_dlls)$" >/dev/null; then
    echo "$exe imports a C runtime DLL" >&2
    return 1
  fi
}

seed_link() { # <target> <object> <executable>
  local target="$1" obj="$2" exe="$3"
  if [[ "$target" == windows-* ]]; then
    # Dash flags survive Git Bash's path conversion. Timestamp and
    # reproducible-build flags keep stage2/stage3 PE headers identical.
    lld-link -subsystem:console -entry:mainCRTStartup "-machine:$seed_link_machine" \
      -stack:8388608 -timestamp:0 -Brepro "-out:$exe" "$obj" "${seed_link_args[@]}" || return 1
    seed_validate_windows "$exe" || return 1
  else
    cc "$obj" -o "$exe" "${seed_link_args[@]}" || return 1
  fi
}
