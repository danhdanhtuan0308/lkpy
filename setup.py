import os
import sys
from pathlib import Path
from setuptools import setup
from distutils.cmd import Command
from distutils import ccompiler, sysconfig
from distutils.command.build import build
from textwrap import dedent
from setuptools._vendor.packaging.requirements import Requirement
import json
try:
    from yaml import dump
except ImportError:
    def dump(obj, f):
        json.dump(obj, f, indent=2)


class DepInfo(Command):
    description = 'get dependency information'
    user_options = [
        ('extras=', 'E', 'extras to include in dependency list'),
        ('all-extras', 'A', 'include all extras'),
        ('ignore-extras=', 'I', 'ignore an extra from all-extras'),
        ('conda-env=', 'c', 'write Conda environment file'),
        ('conda-requires=', None, 'extra Conda requirements (raw yaml)'),
        ('conda-main', None, 'use main Anaconda channel instead of conda-forge'),
        ('blas=', None, 'specify BLAS implementation for Conda environment'),
        ('python-version=', None, 'use specified Python version')
    ]
    boolean_options = ['all-extras', 'conda-main']
    MAIN_PACKAGE_MAP = {
        'nbsphinx': '$pip',
        'nbval': '$pip',
        'implicit': '$pip'
    }
    FORGE_PACKAGE_MAP = {
        'hpfrec': '$pip',
        'csr': '$pip'
    }
    CONDA_EXTRA_DEPS = ['tbb']
    # CONDA_MAIN_DEPS = ['mkl-devel', 'tbb']

    def initialize_options(self):
        """Set default values for options."""
        self.conda_requires = None
        self.extras = None
        self.all_extras = False
        self.ignore_extras = None
        self.conda_env = None
        self.conda_requires = None
        self.conda_main = False
        self.python_version = None
        self.blas = None

    def finalize_options(self):
        """Post-process options."""
        if self.extras is None:
            self.extras = []
        else:
            self.extras = self.extras.split(',')
        if self.ignore_extras is None:
            self.ignore_extras = []
        else:
            self.ignore_extras = self.ignore_extras.split(',')
        if self.all_extras:
            self.extras = [e for e in self.distribution.extras_require.keys()
                           if e not in self.ignore_extras]
        if self.conda_requires is None:
            self.conda_requires = ''
        self.PACKAGE_MAP = self.MAIN_PACKAGE_MAP if self.conda_main else self.FORGE_PACKAGE_MAP

    def run(self):
        if self.conda_env:
            self._write_conda(self.conda_env)
        else:
            for req, src in self._get_reqs():
                if src:
                    msg = f'{req}  # {src}'
                else:
                    msg = req
                print(msg)

    def _write_conda(self, file):
        if self.python_version:
            pyver = f'={self.python_version}'
        else:
            pyver = self.distribution.python_requires
        pip_deps = []
        dep_spec = {
            'name': 'lkpy-dev',
        }
        if self.conda_main:
            dep_spec['channels'] = ['lenskit', 'default']
        else:
            dep_spec['channels'] = ['conda-forge']

        dep_spec['dependencies'] = [
            f'python {pyver}',
            'pip'
        ] + self.CONDA_EXTRA_DEPS
        if self.blas:
            dep_spec['dependencies'].append('libblas=*=*' + self.blas)
            if self.blas == 'mkl':
                dep_spec['dependencies'].append('mkl-devel')
        for req_str, src in self._get_reqs():
            req = Requirement(req_str)
            mapped = self.PACKAGE_MAP.get(req.name, req.name)
            if mapped.startswith('$pip'):
                pip_deps.append(self._pip_dep(req, mapped))
            else:
                dep_spec['dependencies'].append(self._spec_str(req, mapped, src))
        if pip_deps:
            dep_spec['dependencies'].append({
                'pip': pip_deps
            })

        if file == '-':
            dump(dep_spec, sys.stdout)
        else:
            with open(file, 'w') as f:
                dump(dep_spec, f)

    def _spec_str(self, req, name=None, src=None):
        spec = name if name is not None else req.name
        if req.specifier:
            spec += ' ' + str(req.specifier)
        return spec

    def _pip_dep(self, req, key):
        if key != '$pip':
            # it's $pip:
            return key[5:]
        spec = req.name
        if req.specifier:
            spec += ' ' + str(req.specifier)
        return spec

    def _get_reqs(self):
        for req in self.distribution.install_requires:
            yield req, None
        for req in self.distribution.tests_require:
            yield req, 'test'
        for ex in self.extras:
            ereqs = self.distribution.extras_require[ex]
            for req in ereqs:
                yield req, ex


if __name__ == "__main__":
    setup(cmdclass={
        'dep_info': DepInfo
    })
