load("@bazel_tools//tools/cpp:toolchain_utils.bzl", "find_cpp_toolchain")
load(
    "@bazel_tools//tools/build_defs/cc:action_names.bzl",
    "CPP_COMPILE_ACTION_NAME",
)

def _get_file_group(rule_attrs, attr_name):
    file_targets = getattr(rule_attrs, attr_name, None)
    if not file_targets:
        return []
    return [file.path for t in file_targets for file in t.files.to_list()]

def _cc_compiler_info(ctx, target):
    cc_toolchain = find_cpp_toolchain(ctx)
    feature_configuration = cc_common.configure_features(
        ctx = ctx,
        cc_toolchain = cc_toolchain,
        requested_features = ctx.features,
        unsupported_features = ctx.disabled_features,
    )

    compile_variables = cc_common.create_compile_variables(
        feature_configuration = feature_configuration,
        cc_toolchain = cc_toolchain,
        user_compile_flags = ctx.fragments.cpp.cxxopts +
                             ctx.fragments.cpp.copts,
        add_legacy_cxx_options = True,
    )
    compiler_options = cc_common.get_memory_inefficient_command_line(
        feature_configuration = feature_configuration,
        action_name = CPP_COMPILE_ACTION_NAME,
        variables = compile_variables,
    )

    compilation_context = target[CcInfo].compilation_context
    return struct(
        include_dirs = compilation_context.includes.to_list(),
        system_include_dirs = compilation_context.system_includes.to_list(),
        quote_include_dirs = compilation_context.quote_includes.to_list(),
        compile_flags = compiler_options + getattr(ctx.rule.attr, "copts", []),
        defines = compilation_context.defines.to_list(),
    )

def _msbuild_aspect_impl(target, ctx):
    info = struct(
        workspace_root = ctx.label.workspace_root,
        package = ctx.label.package,
        files = struct(**{name: _get_file_group(ctx.rule.attr, name) for name in ["srcs", "hdrs"]}),
        deps = [str(dep.label) for dep in getattr(ctx.rule.attr, "deps", [])],
        target = struct(label = str(target.label), files = [f.path for f in target.files.to_list()]),
        kind = ctx.rule.kind,
        cc = _cc_compiler_info(ctx, target),
    )

    info_file = ctx.actions.declare_file(target.label.name + ".msbuild")
    ctx.actions.write(info_file, info.to_json(), is_executable = False)

    dep_outputs = []
    for dep in getattr(ctx.rule.attr, "deps", []):
        dep_outputs.append(dep[OutputGroupInfo].msbuild_outputs)
    outputs = depset([info_file], transitive = dep_outputs)
    return [OutputGroupInfo(msbuild_outputs = outputs)]

msbuild_aspect = aspect(
    attr_aspects = ["deps"],
    attrs = {
        "_cc_toolchain": attr.label(
            default = Label("@bazel_tools//tools/cpp:current_cc_toolchain"),
        ),
    },
    fragments = ["cpp"],
    toolchains = ["@bazel_tools//tools/cpp:toolchain_type"],
    implementation = _msbuild_aspect_impl,
)
