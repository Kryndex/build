#!/usr/bin/env python
# Copyright 2017 The Fuchsia Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import argparse
import errno
import os
import re
import shutil
import subprocess
import sys
import tempfile


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FUCHSIA_ROOT = os.path.dirname(  # $root
    os.path.dirname(             # build
    SCRIPT_DIR))                 # zircon
ZIRCON_ROOT = os.path.join(FUCHSIA_ROOT, 'zircon')

sys.path += [os.path.join(FUCHSIA_ROOT, "third_party", "mako")]
from mako.lookup import TemplateLookup
from mako.template import Template


# Packages included in the sysroot.
SYSROOT_PACKAGES = ['c', 'zircon']


def make_dir(path, is_dir=False):
    """Creates the directory at `path`."""
    target = path if is_dir else os.path.dirname(path)
    try:
        os.makedirs(target)
    except OSError as exception:
        if exception.errno == errno.EEXIST and os.path.isdir(target):
            pass
        else:
            raise


def try_remove(list, element):
    """Attempts to remove an element from a list, returning `true` if
       successful."""
    try:
        list.remove(element)
        return True
    except ValueError:
        return False


def parse_package(lines):
    """Parses the content of a package file."""
    result = {}
    section_exp = re.compile('^\[([^\]]+)\]$')
    attr_exp = re.compile('^([^=]+)=(.*)$')
    current_section = None
    def finalize_section():
        if not current_section:
            return
        if current_list and current_map:
            raise Error('Found both map-style and list-style section')
        result[current_section] = (current_map if current_map
                                   else current_list)
    for line in lines:
        section_match = section_exp.match(line)
        if section_match:
            finalize_section()
            current_section = section_match.group(1)
            current_list = []
            current_map = {}
            continue
        attr_match = attr_exp.match(line)
        if attr_match:
            name = attr_match.group(1)
            value = attr_match.group(2)
            current_map[name] = value
        else:
            current_list.append(line.strip())
    finalize_section()
    return result


def extract_file(name, path, context):
    """Extracts file path and base folder path from a map entry."""
    # name: foo/bar.h
    # path: <SOURCE|BUILD>/somewhere/under/zircon/foo/bar.h
    (full_path, changes) = re.subn('^SOURCE', context.source_base, path)
    if not changes:
        (full_path, changes) = re.subn('^BUILD', context.build_base, path)
    if not changes:
        raise Exception('Unknown pattern type: %s' % path)
    folder = None
    if full_path.endswith(name):
        folder = os.path.relpath(full_path[:-len(name)], FUCHSIA_ROOT)
    file = os.path.relpath(full_path, FUCHSIA_ROOT)
    return (file, folder)


def filter_deps(deps):
    """Sanitizes a given dependency list."""
    return filter(lambda x: x not in SYSROOT_PACKAGES, deps)


def generate_build_file(path, template_name, data, context):
    """Creates a build file based on a template."""
    make_dir(path)
    template = context.templates.get_template(template_name)
    contents = template.render(data=data)
    with open(path, 'w') as build_file:
        build_file.write(contents)


class SourceLibrary(object):
    """Represents a library built from sources.

       Convenience storage object to be consumed by Mako templates."""

    def __init__(self, name):
        self.name = name
        self.include_dirs = set()
        self.sources = []
        self.deps = []
        self.libs = set()


def generate_source_library(package, context):
    """Generates the build glue for a library whose sources are provided."""
    lib_name = package['package']['name']
    data = SourceLibrary(lib_name)

    # Includes.
    for name, path in package.get('includes', {}).iteritems():
        (file, folder) = extract_file(name, path, context)
        data.sources.append('//%s' % file)
        data.include_dirs.add('//%s' % folder)

    # Source files.
    for name, path in package.get('src', {}).iteritems():
        (file, _) = extract_file(name, path, context)
        data.sources.append('//%s' % file)

    # Dependencies.
    data.deps += filter_deps(package.get('deps', []))
    data.deps += filter_deps(package.get('static-deps', []))

    # Generate the build file.
    build_path = os.path.join(context.out_dir, 'lib', lib_name, 'BUILD.gn')
    generate_build_file(build_path, 'source_library.mako', data, context)


class CompiledLibrary(object):
    """Represents a library already compiled by the Zircon build.

       Convenience storage object to be consumed by Mako templates."""

    def __init__(self, name):
        self.name = name
        self.include_dirs = set()
        self.deps = []
        self.lib_name = ''
        self.is_shared = False
        self.prebuilt = ''
        self.debug_prebuilt = ''


def generate_compiled_library(package, context):
    """Generates the build glue for a prebuilt library."""
    lib_name = package['package']['name']
    data = CompiledLibrary(lib_name)

    # TODO(pylaligand): record and use the architecture.

    # Includes.
    for name, path in package.get('includes', {}).iteritems():
        (file, folder) = extract_file(name, path, context)
        data.include_dirs.add('//%s' % folder)

    # Lib.
    libs = package.get('lib', {})
    if len(libs) == 1:
        # Static library.
        data.is_shared = False
        (name, path) = libs.items()[0]
        (file, _) = extract_file(name, path, context)
        data.prebuilt = "//%s" % file
        data.lib_name = os.path.basename(file)
    elif len(libs) == 2:
        # Shared library.
        data.is_shared = True
        for name, path in libs.iteritems():
            (file, _) = extract_file(name, path, context)
            if '/debug/' in name:
                data.debug_prebuilt = '//%s' % file
                data.lib_name = os.path.basename(file)
            else:
                data.prebuilt = '//%s' % file
    else:
        raise Exception('Too many files for %s: %s' % (lib_name,
                                                       ', '.join(libs.keys())))

    # Dependencies.
    data.deps += filter_deps(package.get('deps', []))

    # Generate the build file.
    build_path = os.path.join(context.out_dir, 'lib', lib_name, 'BUILD.gn')
    generate_build_file(build_path, 'compiled_library.mako', data, context)


class Sysroot(object):
    """Represents the sysroot created by Zircon.

       Convenience storage object to be consumed by Mako templates."""

    def __init__(self):
        self.files = {}


def generate_sysroot(package, context):
    """Generates the build glue for the sysroot."""
    data = Sysroot()

    # Includes.
    for name, path in package.get('includes', {}).iteritems():
        (file, _) = extract_file(name, path, context)
        data.files['include/%s' % name] = '//%s' % file

    # Lib.
    for name, path in package.get('lib', {}).iteritems():
        (file, _) = extract_file(name, path, context)
        data.files[name] = '//%s' % file

    # Generate the build file.
    build_path = os.path.join(context.out_dir, 'sysroot', 'BUILD.gn')
    generate_build_file(build_path, 'sysroot.mako', data, context)


class GenerationContext(object):
    """Describes the context in which GN rules should be generated."""

    def __init__(self, out_dir, source_base, build_base, templates):
        self.out_dir = out_dir
        self.source_base = source_base
        self.build_base = build_base
        self.templates = templates


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out',
                        help='Path to the output directory',
                        required=True)
    parser.add_argument('--zircon-build',
                        help='Path to the Zircon build directory',
                        required=True)
    parser.add_argument('--debug',
                        help='Whether to print out debug information',
                        action='store_true')
    args = parser.parse_args()

    out_dir = os.path.abspath(args.out)
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    debug = args.debug

    # Generate package descriptions through Zircon's build.
    zircon_dir = tempfile.mkdtemp('-zircon-packages')
    if debug:
        print('Building Zircon in: %s' % zircon_dir)
    make_args = [
        'make',
        'packages',
        'BUILDDIR=%s' % zircon_dir,
    ]
    subprocess.check_call(make_args, cwd=ZIRCON_ROOT,
                          env={} if debug else {'QUIET': '1'})

    # Parse package definitions.
    packages = []
    with open(os.path.join(zircon_dir, 'export', 'manifest'), 'r') as manifest:
        package_files = map(lambda line: line.strip(), manifest.readlines())
    for file in package_files:
        with open(os.path.join(zircon_dir, 'export', file), 'r') as pkg_file:
            packages.append(parse_package(pkg_file.readlines()))
    if debug:
        print('Found %s packages:' % len(packages))
        names = sorted(map(lambda p: p['package']['name'], packages))
        for name in names:
            print(' - %s' % name)

    if not debug:
        shutil.rmtree(zircon_dir)

    # Generate some GN glue for each package.
    context = GenerationContext(
        out_dir,
        ZIRCON_ROOT,
        os.path.abspath(args.zircon_build),
        TemplateLookup(directories=[SCRIPT_DIR]),
    )
    for package in packages:
        name = package['package']['name']
        type = package['package']['type']
        arch = package['package']['arch']
        if name in SYSROOT_PACKAGES:
            print('Ignoring sysroot part: %s' % name)
            continue
        if type != 'lib':
            print('(%s) Unsupported package type: %s/%s, skipping'
                  % (name, type, arch))
            continue
        if arch == 'src':
            type = 'source'
            generate_source_library(package, context)
        elif name == 'c':
            type = 'sysroot'
            generate_sysroot(package, context)
        else:
            type = 'prebuilt'
            generate_compiled_library(package, context)
        if debug:
            print('Processed %s (%s)' % (name, type))


if __name__ == "__main__":
    sys.exit(main())
