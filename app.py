"""
A Streamlit app to run and debug conda solves online
"""
import json
import os
import re
import sys
import tarfile
from pathlib import Path
from subprocess import run, TimeoutExpired
from urllib.request import urlretrieve

import streamlit as st

TITLE = "Online solver for conda packages"
ALLOWED_CHANNELS = ["conda-forge", "bioconda", "defaults"]
TTL = 3600  # seconds
REPODATA_TIMEOUT = 60  # seconds
SOLVE_TIMEOUT = 30  # seconds
MAX_CHARS_PER_LINE = 50
MAX_LINES_PER_REQUEST = 25

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
    micromamba_path = Path(__file__).parent / "micromamba"
    if micromamba_path.is_file():
        return micromamba_path
    url = f"https://conda.anaconda.org/conda-forge/{_platform()}/micromamba-1.5.1-0.tar.bz2"
    tarball, _ = urlretrieve(url)
    with tarfile.open(tarball, "r:bz2") as tf:
        for item in tf:
            if item.name == "bin/micromamba":
                item.name = "micromamba"
                tf.extract(item, path=Path(__file__).parent)
                break
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
        "--json",
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

    p = run(cmd, capture_output=True, text=True, timeout=SOLVE_TIMEOUT, env=env)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        pass
    return {
        "success": False,
        "stdout": p.stdout,
        "stderr": p.stderr,
        "returncode": p.returncode,
        "cmd": cmd,
        "virtual_packages": virtual_packages or {},
    }


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
    lines = [f"# subdir: {platform}", "@EXPLICIT"]
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
        raise ValueError(f"Spaces not allowed in package specifications: `{line}`")
    if "::" in line and (channel := line.split("::")[0]) not in ALLOWED_CHANNELS:
        raise ValueError(
            f"Specified channel `{channel}` is not allowed. "
            f"Use one of: {', '.join(ALLOWED_CHANNELS)}."
        )
    if line.startswith("-"):
        raise ValueError(f"Invalid package specification: `{line}`.")
    if line.lower().startswith(("http://", "https://", "file://", "ftp://", "s3://")):
        raise ValueError(f"URLs not allowed: `{line}`.")
    if line.startswith("*"):
        raise ValueError(f"Wildcards not allowed: `{line}`.")
    if not re.match(r"^[a-zA-Z0-9_\-\.\*=!><,|;\[\]/]+$", line):
        raise ValueError(f"Invalid characters in package specification: `{line}`.")
    return line


def validate_packages(packages):
    if len(packages) > MAX_LINES_PER_REQUEST:
        raise ValueError(
            f"Too many packages requested. Maximum is {MAX_LINES_PER_REQUEST}. "
        )
    for pkg in packages:
        pkg = validate_package(pkg)
        if pkg:
            yield pkg


# ---
# Streamlit app


st.title(f":snake: {TITLE}", anchor="top")

with st.sidebar:
    st.sidebar.title("Options")
    platform = st.selectbox(
        "Platform",
        ["linux-64", "linux-aarch64", "linux-ppc64le", "osx-64", "osx-arm64", "win-64"],
    )
    channels = st.multiselect("Channels", ALLOWED_CHANNELS, placeholder="conda-forge")
    packages = st.text_area(
        "Packages",
        help=f"Specify up to {MAX_LINES_PER_REQUEST} packages, one per line.",
        placeholder="python=3\nnumpy>=1.18.1=*py38*\nscipy[build=*py38*]",
        max_chars=MAX_CHARS_PER_LINE * MAX_LINES_PER_REQUEST,
    )
    with st.expander("Advanced"):
        priority = st.selectbox("Channel priority", ["strict", "flexible", "disabled"])
        virtual_packages = {}
        if platform.startswith("linux"):
            virtual_packages["linux"] = st.text_input("`__linux`", "1", disabled=True)
            virtual_packages["glibc"] = st.text_input(
                "`__glibc`",
                "2.12",
                max_chars=10,
            )
            virtual_packages["cuda"] = st.text_input("`__cuda`", "11.0", max_chars=10)
        elif platform.startswith("osx"):
            virtual_packages["osx"] = st.text_input("`__osx`", "10.9", max_chars=10)
        elif platform.startswith("win"):
            virtual_packages["win"] = st.text_input("`__win`", "1", disabled=True)
            virtual_packages["cuda"] = st.text_input("`__cuda`", "11.0", max_chars=10)

    ok = st.sidebar.button("Run solve")

if ok or (packages and channels and platform):
    try:
        specs = list(validate_packages(packages.splitlines()))
        result = solve(
            sorted(specs),
            channels=channels,
            platform=platform,
            priority=priority,
            virtual_packages=virtual_packages,
        )
    except ValueError as e:
        st.error(e)
        st.stop()
    except TimeoutExpired:
        st.error(
            "Solver timed out. Try again with a simpler request "
            "(e.g. fewer packages, or more specific specs)."
        )
        st.stop()
    except Exception as e:
        raise
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
        st.markdown("### Solver found conflicts")
        for problem in problems:
            st.error(f"`{problem}`")
    else:
        st.error("Unknown error. Check the full JSON result below.")
    with st.expander("Full JSON result"):
        st.code(json.dumps(result, indent=2), language="json")
