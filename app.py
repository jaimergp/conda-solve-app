"""
A Streamlit app to run and debug conda solves online
"""
import json
import os
import re
import sys
import tarfile
import time
from pathlib import Path
from subprocess import run, TimeoutExpired
from urllib.request import urlretrieve

import streamlit as st

TITLE = "Online solver for conda packages"
ALLOWED_CHANNELS = ["conda-forge", "bioconda", "defaults"]
ALLOWED_PLATFORMS = [
    "linux-64",
    "linux-aarch64",
    "linux-ppc64le",
    "osx-64",
    "osx-arm64",
    "win-64",
]
ALLOWED_PRIORITIES = ["strict", "flexible", "disabled"]
TTL = 3600  # seconds
REPODATA_TIMEOUT = 60  # seconds
SOLVE_TIMEOUT = 30  # seconds
MAX_CHARS_PER_LINE = 50
MAX_LINES_PER_REQUEST = 25

STATEFUL_KEYS = ("platform", "channels", "packages", "priority", "glibc", "cuda", "osx")
DEFAULT_STATE = {
    "platform": "linux-64",
    "priority": "strict",
    "glibc": "2.17",
    "cuda": "12.0",
    "osx": "11.0",
}

st.set_page_config(
    page_title=TITLE,
    page_icon=":snake:",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _platform():
    operating_system = {
        "linux": "linux",
        "linux2": "linux",
        "darwin": "osx",
    }[sys.platform]
    arch = {
        "x86_64": "64",
        "aarch64": "aarch64",
        "ppc64le": "ppc64le",
        "arm64": "arm64",
    }[os.uname().machine]
    return f"{operating_system}-{arch}"


@st.cache_resource
def micromamba():
    workdir = Path(__file__).parent
    micromamba_path = workdir / "micromamba"
    if micromamba_path.is_file():
        return micromamba_path
    version = "1.5.1-0"
    url = (
        "https://github.com/mamba-org/micromamba-releases/releases/download/"
        f"{version}/micromamba-{_platform()}"
    )
    urlretrieve(url, micromamba_path)
    os.chmod(str(micromamba_path), 0o755)
    return micromamba_path


@st.cache_data(ttl=TTL)
def refresh_repodata(channels, platform):
    cmd = [
        micromamba(),
        "create",
        "--dry-run",
        "--name",
        "test",
        "--repodata-ttl",
        f"{TTL}",
        "--platform",
        platform,
        "--override-channels",
        "xz",
    ]
    for channel in channels:
        cmd += ["--channel", channel]

    p = run(cmd, capture_output=True, text=True, timeout=REPODATA_TIMEOUT)
    if p.returncode != 0:
        st.warning(f"Failed to refresh repodata! {p.stderr}")


@st.cache_data(ttl=TTL)
def solve(
    packages,
    channels=("conda-forge",),
    platform="linux-64",
    priority="strict",
    virtual_packages=None,
):
    refresh_repodata(sorted(channels), platform)
    cmd = [
        micromamba(),
        "create",
        "--dry-run",
        "--name",
        "test",
        "--repodata-ttl",
        f"{TTL}",
        "--platform",
        platform,
        "--override-channels",
        "--channel-priority",
        priority,
    ]
    for channel in channels:
        cmd += ["--channel", channel]
    cmd += packages
    env = os.environ.copy()
    for k, v in (virtual_packages or {}).items():
        if v:
            env["CONDA_OVERRIDE_" + k.upper()] = v
    t0 = time.time()
    p = run(
        [*cmd, "--json"], capture_output=True, text=True, timeout=SOLVE_TIMEOUT, env=env
    )
    time_taken = time.time() - t0
    try:
        result = json.loads(p.stdout)
    except json.JSONDecodeError:
        return {
            "success": False,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "returncode": p.returncode,
            "cmd": cmd,
            "virtual_packages": virtual_packages or {},
            "stats": {"time_taken": time_taken},
        }
    result["stats"] = {"time_taken": time_taken}
    if result.get("solver_problems"):
        # augment with explained problems, not available in JSON output :shrug:
        p = run(
            [*cmd, "--quiet"],
            capture_output=True,
            text=True,
            timeout=SOLVE_TIMEOUT,
            env=env,
        )
        error_lines = None
        for line in p.stderr.splitlines():
            line = line.rstrip()
            if "Could not solve for environment specs" in line:
                if error_lines is None:
                    error_lines = []
                    continue
                break
            if error_lines is not None:
                error_lines.append(line)
        result["explained_problems"] = "\n".join(error_lines)
        return result
    return result


def _readable_size(num, suffix="B"):
    "https://stackoverflow.com/a/1094933"
    for unit in ("", "Ki", "Mi", "Gi", "Ti", "Pi", "Ei", "Zi"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}{suffix}"
        num /= 1024.0
    return f"{num:.1f} Yi{suffix}"


def result_table(packages, specs):
    table = [
        "| Name | Version | Build | Subdir | Channel | Size |",
        "|:-----|:------- |:------|:-------|---------|:------:|",
    ]
    total_size = 0
    spec_names = set()
    for spec in specs:
        name = (
            spec.split("=")[0]
            .split(">")[0]
            .split("<")[0]
            .split("!")[0]
            .split("::")[-1]
            .lower()
        )
        spec_names.add(name.lower())

    for pkg in sorted(packages, key=lambda x: x["name"]):
        name = pkg["name"]
        table.append(
            " | ".join(
                [
                    (f"**{name}**" if name.lower() in spec_names else name),
                    f"`{pkg['version']}`",
                    f"`{pkg['build']}`",
                    f"`{pkg['subdir']}`",
                    f"[{pkg['channel'].rsplit('/', 2)[-2]}]({pkg['url']})",
                    f"`{_readable_size(pkg['size'])}`",
                ],
            )
        )
        total_size += pkg["size"]
    table.append(
        f" **{len(packages)} packages** | | | "
        f"| **Total size:** | **{_readable_size(total_size)}**"
    )
    return "\n".join(table)


def lockfile(packages, platform):
    lines = [
        "# This file may be used to create an environment using:",
        "# $ conda create --name <env> --file <this file>",
        f"# platform: {platform}",
        "@EXPLICIT",
    ]
    for pkg in packages:
        lines.append(f"{pkg['url']}#{pkg['md5']}")
    return "\n".join(lines)


def validate_package(line):
    line = line.strip().lower()
    if line.startswith("#"):
        return
    if not line:
        return
    if len(line) > MAX_CHARS_PER_LINE:
        raise ValueError("Line too long.")
    if " " in line:
        raise ValueError(f"Spaces not allowed in package spec: `{line}`")
    if "::" in line and (channel := line.split("::")[0]) not in ALLOWED_CHANNELS:
        raise ValueError(
            f"Specified channel `{channel}` is not allowed. "
            f"Use one of: {', '.join(ALLOWED_CHANNELS)}."
        )
    if line.startswith("-"):
        raise ValueError(f"Invalid package spec: `{line}`.")
    if line.lower().startswith(("http://", "https://", "file://", "ftp://", "s3://")):
        raise ValueError(f"URLs not allowed: `{line}`.")
    if line.startswith("*"):
        raise ValueError(f"Wildcards not allowed: `{line}`.")
    if not re.match(r"^[a-zA-Z0-9_\-\.\*=!><,|;\[\]/]+$", line):
        raise ValueError(f"Invalid characters in package spec: `{line}`.")
    return line


def validate_packages(packages):
    if len(packages) > MAX_LINES_PER_REQUEST:
        raise ValueError(
            f"Too many packages requested. Maximum is {MAX_LINES_PER_REQUEST}. "
        )
    pkgs = []
    for pkg in packages:
        pkg = validate_package(pkg)
        if pkg:
            pkgs.append(pkg)
    if not pkgs:
        raise ValueError("No valid packages specified.")
    return pkgs


def parse_url_params():
    parsed = {}
    url_params = st.experimental_get_query_params()
    for key in STATEFUL_KEYS:
        value = url_params.get(key, [None])[0]
        if value:
            if key == "channels":
                value = list(dict.fromkeys(value.split(",")))
                bad_channel = False
                for channel in value:
                    if channel not in ALLOWED_CHANNELS:
                        st.error(
                            f"Invalid channel `{channel}`. "
                            f"Use one of: {', '.join(ALLOWED_CHANNELS)}."
                        )
                        bad_channel = True
                if bad_channel:
                    continue
            elif key == "platform" and value not in ALLOWED_PLATFORMS:
                st.error(
                    f"Invalid platform `{value}`. "
                    f"Use one of: {', '.join(ALLOWED_PLATFORMS)}."
                )
                continue
            elif key == "priority" and value not in ALLOWED_PRIORITIES:
                st.error(
                    f"Invalid channel priority `{value}`. "
                    f"Use one of: {', '.join(ALLOWED_PRIORITIES)}."
                )
                continue
            elif key in ("glibc", "cuda", "osx") and not re.match(r"^[0-9\.]+$", value):
                st.error(f"Invalid value for `{key}`: `{value}`")
                continue
            elif key == "packages" and len(value.splitlines()) > MAX_LINES_PER_REQUEST:
                st.error(
                    f"Too many packages requested. Maximum is {MAX_LINES_PER_REQUEST}."
                )
                continue
            parsed[key] = value
            url_params.pop(key)
    return parsed, url_params


def initialize_state():
    parsed_url_params, invalid = parse_url_params()
    if invalid:
        st.error(f"Invalid URL params: `{', '.join(invalid)}`")
        return True

    # Initialize state from URL params, only on first run
    # These state keys match the sidebar widgets keys below
    for key in STATEFUL_KEYS:
        if key in st.session_state:
            continue  # only define once per session
        value = parsed_url_params.get(key) or DEFAULT_STATE.get(key)
        if value:
            setattr(st.session_state, key, value)


# ---
# Streamlit app starts here

initialization_error = initialize_state()
st.title(f":snake: {TITLE}", anchor="top")

with st.sidebar:
    st.sidebar.title("Options")
    platform = st.selectbox(
        "Platform *",
        ALLOWED_PLATFORMS,
        key="platform",
    )
    channels = st.multiselect(
        "Channels *",
        ALLOWED_CHANNELS,
        placeholder="Pick at least one",
        key="channels",
    )
    packages = st.text_area(
        "Packages *",
        help=f"Specify up to {MAX_LINES_PER_REQUEST} packages, one per line.",
        placeholder="python=3\nnumpy>=1.18.1=*py38*\nscipy[build=*py38*]",
        max_chars=MAX_CHARS_PER_LINE * MAX_LINES_PER_REQUEST,
        key="packages",
    )
    with st.expander("Advanced"):
        priority = st.selectbox(
            "Channel priority", ALLOWED_PRIORITIES, key="priority"
        )
        virtual_packages = {}
        if platform.startswith(("linux-", "win-")):
            virtual_packages["cuda"] = st.text_input(
                "`__cuda`", DEFAULT_STATE["cuda"], max_chars=10, key="cuda"
            )
        if platform.startswith("linux-"):
            virtual_packages["linux"] = st.text_input("`__linux`", "1", disabled=True)
            virtual_packages["glibc"] = st.text_input(
                "`__glibc`", DEFAULT_STATE["glibc"], max_chars=10, key="glibc"
            )
        elif platform.startswith("osx-"):
            virtual_packages["osx"] = st.text_input("`__osx`", DEFAULT_STATE["osx"], max_chars=10, key="osx")
        elif platform.startswith("win-"):
            virtual_packages["win"] = st.text_input("`__win`", "1", disabled=True)

    enabled = all([platform, channels, packages.strip()])
    ok = st.sidebar.button("Run solve", disabled=not enabled)

if ok or enabled:
    try:
        specs = validate_packages(packages.splitlines())
    except ValueError as e:
        st.error(e)
        st.stop()
    st.experimental_set_query_params(
        platform=platform,
        channels=",".join(channels),
        packages="\n".join(specs),
        priority=priority,
        **{k: v for k, v in virtual_packages.items() if v and k in STATEFUL_KEYS},
    )
    try:
        result = solve(
            sorted(specs),
            channels=channels,
            platform=platform,
            priority=priority,
            virtual_packages=virtual_packages,
        )
    except TimeoutExpired:
        st.error(
            "Solver timed out. Try again with a simpler request "
            "(e.g. fewer packages, or more specific specs)."
        )
        st.stop()
    except Exception as e:
        st.error(f"Unknown error! {e.__class__.__name__}: {e}")
        st.stop()

    if result.get("success"):
        records = result["actions"]["LINK"]
        st.markdown("### Results table")
        st.markdown(result_table(records, specs))
        st.markdown("")
        with st.expander("Lockfile"):
            st.code(lockfile(records, platform), language="text")
    elif problems := result.get("solver_problems"):
        st.error("Solver could not find a solution!")
        st.code("\n".join(problems), language="text")
        if explained := result.get("explained_problems"):
            st.code(explained, language="text")
    else:
        st.error("Unknown error. Check the full JSON result below.")
    with st.expander("Full JSON result"):
        st.code(json.dumps(result, indent=2), language="json")
    st.markdown(f"> ⌛️ _Solver took {result['stats']['time_taken']:.3f} seconds_.")
elif initialization_error:
    st.info("There were errors initializing the app. Check your URL.")
else:
    st.experimental_set_query_params()
    st.info(
        "Use the left sidebar to specify your input. "
        "Fields marked with * are required."
    )
