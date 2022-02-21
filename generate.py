"""Generates Visual Studio project files."""

from __future__ import division, print_function, unicode_literals
from collections import namedtuple, OrderedDict
import argparse
import dataclasses
import errno
import json
import locale
import os
import pathlib
import re
import subprocess
import sys

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
BAZEL = os.environ.get("BAZEL", "bazel")

PROJECT_TYPE_GUID = "{8BC9CEB8-8B4A-11D0-8D11-00A0C91BC942}"


class Label:
    PATTERN = re.compile(
        r"((@[a-zA-Z0-9/._-]+)?//)?([a-zA-Z0-9/._-]*)(:([a-zA-Z0-9_/.+=,@~-]+))?$"
    )

    def __init__(self, name):
        match = re.match(Label.PATTERN, name)
        if not match:
            raise ValueError("Invalid label: " + name)
        self.repo = match.group(2) or None
        self._absolute = True if match.group(1) else False
        if not self._absolute:
            raise NotImplementedError("Absolute package path required")
        self.package = match.group(3)
        self.name = match.group(5) or self.package.split("/")[-1]

    @property
    def absolute(self):
        return (self.repo or "") + "//" + self.package + ":" + self.name

    @property
    def package_path(self):
        assert self._absolute
        return os.path.normpath(self.package)

    @property
    def info_path(self):
        """Path to the msbuild info file for this label, relative to the bin path."""
        if self.repo:
            raise NotImplementedError("External repos")
        # TODO: absolute
        return os.path.join(self.package, self.name + ".msbuild")


class Struct:
    pass


class ProjectInfo:
    def __init__(self, label, info_dict):
        self.label = label
        self.rule = Struct()
        self.rule.kind = info_dict["kind"]
        self.rule.srcs = info_dict["files"]["srcs"]
        self.rule.hdrs = info_dict["files"]["hdrs"]
        self.output_files = info_dict["target"]["files"]
        self.guid = _generate_uuid_from_data(str(label))

        if self.output_files:
            output_file = self.output_files[0]
            self.output_file = Struct()
            self.output_file.path = os.path.dirname(output_file)
            self.output_file.basename = os.path.basename(output_file)
        else:
            self.output_file = None

        self._cc = info_dict.get("cc", None)

    @property
    def compile_flags_joined(self):
        return " ".join(self._cc["compile_flags"]) if self._cc else ""

    @property
    def defines_joined(self):
        return ";".join(self._cc["defines"]) if self._cc else ""

    def include_dirs_joined(self, cfg):
        cc = self._cc
        if not cc:
            return ""
        paths = (
            cc["include_dirs"] + cc["system_include_dirs"] + cc["quote_include_dirs"]
        )
        paths = (self._rewrite_include_path(cfg, path) for path in paths)
        return ";".join(paths)

    def _rewrite_include_path(self, cfg: "Configuration", path):
        path = path.replace("/", "\\").split("\\")  # MSYS2 confuses Python
        path = [
            node if node != cfg.default_cfg_dirname else "%(BazelCfgDirname)"
            for node in path
        ]
        return os.path.normpath(os.path.join(cfg.paths.workspace, *path))


BuildConfig = namedtuple("BuildConfig", ["msbuild_name", "bazel_name"])
PlatformConfig = namedtuple("PlatformConfig", ["msbuild_name", "bazel_name"])


@dataclasses.dataclass
class BazelInfo:
    bin: pathlib.Path
    out: pathlib.Path
    workspace: pathlib.Path

    @classmethod
    def from_bazel(cls):
        def cleanup(output):
            for line in output.splitlines():
                (key, value) = line.split(": ", maxsplit=1)
                yield (key.strip(), value.strip())

        def parse(output):
            for (key, value) in cleanup(output):
                if key == "bazel-bin":
                    yield ("bin", pathlib.Path(value))
                elif key == "output_path":
                    yield ("out", pathlib.Path(value))
                elif key == "workspace":
                    yield ("workspace", pathlib.Path(value))

        output = subprocess.check_output(
            (BAZEL, "info", "bazel-bin", "output_path", "workspace"),
            encoding="utf-8",
            errors="replace",
        )
        return cls(**dict(parse(output)))


class Configuration:
    def __init__(self, args):
        self.output_path = args.output.resolve()
        self.paths = BazelInfo.from_bazel()

        self._setup_env()
        self._build_target_list(args)

        self.solution_name = args.solution or os.path.basename(os.getcwd())

        self.build_configs = [
            BuildConfig("Fastbuild", "fastbuild"),
            BuildConfig("Debug", "dbg"),
            BuildConfig("Release", "opt"),
        ]
        self.platforms = [PlatformConfig("x64", "x64_windows")]
        self.user_config_names = args.config or []

        self.system_paths = os.environ["PATH"].split(os.pathsep)
        self._cygpath = self._find_exe("cygpath.exe")
        self.bazel_path = self.canonical_path(
            self._find_exe("bazel.exe") or self._find_exe(BAZEL)
        )

        self.default_cfg_dirname = "x64_windows-fastbuild"

    def _build_target_list(self, args):
        # If no query, use all targets in the workspace.
        queries = args.query or ["//..."]

        kinds = set(["cc_library", "cc_inc_library", "cc_binary", "cc_test"])

        # Use OrderedDict to eliminate duplicates, but keep ordering
        targets = OrderedDict()
        for query in queries:
            for target in self._get_targets_from_query(query, kinds):
                targets[target] = True
        self.targets = targets.keys()

    _LABEL_KIND_PATTERN = re.compile(r"(\w+) rule (.+)$")

    def _get_targets_from_query(self, query, kinds):
        labels = []
        target_list = subprocess.check_output(
            [BAZEL, "query", query, "--output=label_kind"]
        )
        for line in target_list.split(b"\n"):
            line = line.decode("utf-8").strip()
            if not line:
                continue
            match = re.match(Configuration._LABEL_KIND_PATTERN, line.strip())
            if not match:
                raise ValueError("Invalid bazel query output: " + line.strip())
            kind = match.group(1)
            label = match.group(2)
            if kind in kinds:
                labels.append(label)
        return labels

    def _setup_env(self):
        """Modifies the env vars of the process for bazel to run successfully."""
        # Tell MSYS2 not to rewrite absolute package paths in command line args.
        # Don't override a more aggressive setting.
        if os.environ.get("MSYS2_ARG_CONV_EXCL") != "*":
            os.environ["MSYS2_ARG_CONV_EXCL"] = "//"

    def _find_exe(self, name):
        for path in self.system_paths:
            program = os.path.join(path, name)
            if os.path.isfile(program) and os.access(program, os.X_OK):
                return program
        return None

    def canonical_path(self, path):
        """Returns the OS canonical path (i.e. Windows-style path if in Cygwin)."""
        if self._cygpath:
            out = subprocess.check_output([self._cygpath, "-w", path]).strip()
            if isinstance(out, bytes):
                out = out.decode(locale.getpreferredencoding()).strip()
            return out

        return os.path.normpath(path)


def run_aspect(cfg):
    """Invokes bazel on our aspect to generate target info."""
    subprocess.check_call(
        [
            BAZEL,
            "build",
            "--override_repository=bazel-msbuild={}".format(SCRIPT_DIR / "bazel"),
            "--aspects=@bazel-msbuild//bazel-msbuild:msbuild.bzl%msbuild_aspect",
            "--output_groups=msbuild_outputs",
        ]
        + list(cfg.targets)
    )


def read_info(cfg: Configuration, target):
    """Reads the generated msbuild info file for the given target."""
    info_dict = json.load(open(os.path.join(cfg.paths.bin, target.info_path)))
    return ProjectInfo(target, info_dict)


def _msb_nmake_output(target, cfg):
    if not target.output_file:
        return ""
    return (
        r"<NMakeOutput>{cfg.paths.out}\$(BazelCfgDirname)\bin\{target.label.package_path}"
        + r"\{target.output_file.basename}</NMakeOutput>"
    ).format(target=target, cfg=cfg)


def _msb_target_name_ext(target):
    if not target.output_file:
        return ""
    if "." in target.output_file.basename:
        name, ext = target.output_file.basename.rsplit(".", 1)
    else:
        name, ext = target.output_file.basename, ""
    return r"<TargetName>{}</TargetName><TargetExt>{}</TargetExt>".format(name, ext)


def _add_filter_to_set(filters, filter_name):
    """Adds a filter, and all its parent filters to the set `filters`."""
    DELIM = "\\"
    if filter_name in filters:
        return
    components = filter_name.split(DELIM)
    path = components[0]
    filters.add(path)
    for component in components[1:]:
        path += DELIM + component
        filters.add(path)


def _msb_file_filter(info, filename, filters):
    # In most cases, files in a package are in the same directory as that package.
    # If they are in another directory, we add a filter to the file to help
    # keep things organized the same way they are in the codebase.

    # During generation of main project file, we don't include filters info.
    if filters is None:
        return ""

    # During generation of filters file, we return None to indicate not to
    # generate any contet for this file.
    # TODO: This is really hacky!
    dirname = os.path.dirname(filename)
    if not dirname:
        return None
    filter_name = os.path.relpath(dirname, info.label.package_path).replace("/", "\\")
    if not filter_name or filter_name == ".":
        return None
    _add_filter_to_set(filters, filter_name)
    return "<Filter>{}</Filter>".format(filter_name)


def _msb_cc_src(cfg: Configuration, info, filters, filename):
    filter = _msb_file_filter(info, filename, filters)
    if filter is None:
        return None
    return '<ClCompile Include="{name}">{filter}</ClCompile>'.format(
        name=cfg.paths.workspace / filename, filter=filter
    )


def _msb_cc_inc(cfg: Configuration, info, filters, filename):
    filter = _msb_file_filter(info, filename, filters)
    if filter is None:
        return None
    return '<ClInclude Include="{name}">{filter}</ClInclude>'.format(
        name=cfg.paths.workspace / filename, filter=filter
    )


def _msb_item_group(cfg: Configuration, info, filters, file_targets, func):
    if not file_targets:
        return ""
    xml_items = [func(cfg, info, filters, f) for f in file_targets]
    return (
        "\n  <ItemGroup>"
        + "\n    ".join([""] + [item for item in xml_items if item is not None])
        + "\n  </ItemGroup>"
    )


def _msb_files(cfg: Configuration, info, filters=None):
    """Set filters to a set-like when writing filters. All filters used will be added to the set."""
    return _msb_item_group(
        cfg, info, filters, info.rule.srcs, _msb_cc_src
    ) + _msb_item_group(cfg, info, filters, info.rule.hdrs, _msb_cc_inc)


def _sln_project(project):
    # This first UUID appears to be an identifier for Visual C++ packages?
    return 'Project("{type_guid}") = "{name}", "{package}\\{name}.vcxproj", "{guid}"\nEndProject'.format(
        guid=project.guid,
        type_guid=PROJECT_TYPE_GUID,
        name=project.label.name,
        package=project.label.package,
    )


def _sln_projects(projects):
    return "\n".join([_sln_project(project) for project in projects])


def _sln_cfgs(cfg):
    lines = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            lines.append(
                "{cfg}|{platform} = {cfg}|{platform}".format(
                    cfg=build_config.msbuild_name, platform=platform.msbuild_name
                )
            )
    return "\n\t\t".join(lines)


def _sln_project_cfgs(cfg: Configuration, projects):
    lines = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            for project in projects:
                fmt = {
                    "guid": project.guid,
                    "cfg": build_config.msbuild_name,
                    "platform": platform.msbuild_name,
                }
                lines.extend(
                    [
                        "{guid}.{cfg}|{platform}.ActiveCfg = {cfg}|{platform}".format(
                            **fmt
                        ),
                        "{guid}.{cfg}|{platform}.Build.0 = {cfg}|{platform}".format(
                            **fmt
                        ),
                    ]
                )
    return "\n\t\t".join(lines)


def _msb_project_cfgs(cfg):
    configs = []
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            configs.append(
                r"""
    <ProjectConfiguration Include="{cfg}|{platform}">
      <Configuration>{cfg}</Configuration>
      <Platform>{platform}</Platform>
    </ProjectConfiguration>""".format(
                    cfg=build_config.msbuild_name, platform=platform.msbuild_name
                )
            )
    return "".join(configs)


def _msb_cfg_properties(cfg):
    props = []
    user_config = "".join(" --config=" + name for name in cfg.user_config_names)
    for build_config in cfg.build_configs:
        for platform in cfg.platforms:
            props.append(
                r"""
  <PropertyGroup Condition="'$(Configuration)|$(Platform)'=='{cfg.msbuild_name}|{platform.msbuild_name}'">
    <BazelCfgOpts>-c {cfg.bazel_name}{user_config}</BazelCfgOpts>
    <BazelCfgDirname>{platform.bazel_name}-{cfg.bazel_name}</BazelCfgDirname>
  </PropertyGroup>""".format(
                    cfg=build_config, platform=platform, user_config=user_config
                )
            )
    return "\n".join(props)


def _msb_filter_items(filters):
    tags = [
        r"""
    <Filter Include="{name}">
      <UniqueIdentifier>{uuid}</UniqueIdentifier>
    </Filter>""".format(
            name=name, uuid=_generate_uuid_from_data(name)
        )
        for name in filters
    ]
    return "\n".join(tags)


def _generate_uuid_from_data(data):
    # We don't comply with any UUID standard, but we use 3 to advertise that it is a deterministic
    # hash of a name. I don't think Visual Studio will complain about the method used to create our
    # one-way hash.
    # TODO: Actually use more bits.
    hsh = abs(hash(data))
    part1 = hsh // (2 ** 32)
    part2 = hsh % (2 ** 32)
    return "{{{:08X}-0000-3000-A000-0000{:08X}}}".format(part1, part2)


def _makedirs(path):
    """Ensures that the directories in path exist. Does nothing if they do."""
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def _generate_project_filters(filters_template, cfg: Configuration, info):
    filters = set()
    file_groups = _msb_files(cfg, info, filters)

    return filters_template.format(
        file_groups=file_groups, filter_items=_msb_filter_items(filters)
    )


def generate_projects(cfg):
    with open(SCRIPT_DIR / "templates" / "vcxproj.xml") as f:
        template = f.read()
    with open(SCRIPT_DIR / "templates" / "vcxproj.filters.xml") as f:
        filters_template = f.read()
    project_configs = _msb_project_cfgs(cfg)
    config_properties = _msb_cfg_properties(cfg)

    project_infos = []
    for target in cfg.targets:
        info = read_info(cfg, Label(target))
        project_infos.append(info)

        project_dir = cfg.output_path / info.label.package
        content = template.format(
            cfg=cfg,
            target=info,
            target_name_ext=_msb_target_name_ext(info),
            project_configs=project_configs,
            config_properties=config_properties,
            outputs=";".join([os.path.basename(f) for f in info.output_files]),
            file_groups=_msb_files(cfg, info),
            nmake_output=_msb_nmake_output(info, cfg),
            include_dirs_joined=info.include_dirs_joined(cfg),
        )
        filters_content = _generate_project_filters(filters_template, cfg, info)

        path = project_dir / (info.label.name + ".vcxproj")
        path.parent.mkdir(exist_ok=True, parents=True)
        with open(path, "w") as out:
            out.write(content)
        with open(
            os.path.join(project_dir, info.label.name + ".vcxproj.filters"), "w"
        ) as out:
            out.write(filters_content)

    return project_infos


def generate_solution(cfg: Configuration, project_infos):
    with open(SCRIPT_DIR / "templates" / "solution.sln") as f:
        template = f.read()
    cfg.output_path.mkdir(exist_ok=True, parents=True)
    sln_filename = cfg.output_path / (cfg.solution_name + ".sln")
    content = template.format(
        projects=_sln_projects(project_infos),
        cfgs=_sln_cfgs(cfg),
        project_cfgs=_sln_project_cfgs(cfg, project_infos),
        guid=_generate_uuid_from_data(sln_filename),
    )
    with open(sln_filename, "w") as out:
        out.write(content)


def main(argv):
    parser = argparse.ArgumentParser(
        description="Generates Visual Studio project files from Bazel projects."
    )
    parser.add_argument(
        "query",
        nargs="*",
        help="Target query to generate project for [default: all targets]",
    )
    parser.add_argument(
        "--output", "-o", type=pathlib.Path, default="msbuild", help="Output directory"
    )
    parser.add_argument(
        "--solution",
        "-n",
        type=str,
        help="Solution name [default: current directory name]",
    )
    parser.add_argument(
        "--config",
        action="append",
        help="Additional --config option to pass to bazel; may be used multiple times",
    )
    args = parser.parse_args(argv[1:])

    cfg = Configuration(args)

    run_aspect(cfg)
    project_infos = generate_projects(cfg)
    generate_solution(cfg, project_infos)


if __name__ == "__main__":
    main(sys.argv)
